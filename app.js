const $ = (id) => document.getElementById(id);
const api = async (path, method = "GET", body = null) => {
  const opt = { method, headers: {} };
  if (body) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  return r.json();
};
const esc = (s) => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---- tabs ----
document.querySelectorAll(".tab").forEach(t => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    $(t.dataset.tab).classList.add("active");
  };
});

// ---- status polling ----
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const el = $("status");
    if (!s.reachable) { el.textContent = "projector unreachable"; el.className = "status err"; }
    else {
      const play = s.playback && s.playback.playing ? " · playing" : "";
      el.textContent = `${s.model || "projector"} — ${s.power}${play}`;
      el.className = "status " + (s.power === "on" ? "on" : "off");
    }
    // populate input dropdowns once
    if (s.inputs && !$("inputSel").dataset.filled) {
      for (const sel of [$("inputSel"), $("sInput")]) {
        s.inputs.forEach(i => { const o = document.createElement("option"); o.value = o.textContent = i; sel.appendChild(o); });
      }
      $("inputSel").dataset.filled = "1";
    }
    // feature availability hints
    if (s.features && !window._featHinted) {
      window._featHinted = true;
      if (!s.features.ffmpeg) $("dlHint").innerHTML += ' <b class="warn">ffmpeg not found — thumbnails/merging disabled.</b>';
      if (!s.features.ytdlp) $("dlHint").innerHTML += ' <b class="warn">yt-dlp not installed — downloads disabled.</b>';
    }
  } catch (e) {
    $("status").textContent = "server unreachable"; $("status").className = "status err";
  }
}

// ---- control actions ----
const power = (on) => api("/api/power", "POST", { on });
const shutter = (open) => api("/api/shutter", "POST", { open });
const mute = (on) => api("/api/mute", "POST", { on });
const freeze = (on) => api("/api/freeze", "POST", { on });
const selectInput = () => api("/api/input", "POST", { input: $("inputSel").value });
const lens = (axis, direction) =>
  api("/api/lens", "POST", { axis, direction, action: "jog", seconds: parseFloat($("lensStep").value) });
const lensStopAll = () => api("/api/lens", "POST", { axis: "zoom", action: "stop_all" });

async function readLens() {
  const out = [];
  for (const a of ["zoom", "focus", "shift_h", "shift_v"]) {
    const p = await api("/api/lens/position/" + a);
    if (p.axis) out.push(`${a.padEnd(8)} cur=${p.current}  (min ${p.min} / max ${p.max})`);
  }
  $("lensReadout").textContent = out.join("\n") || "no data";
}
const lensMem = (op) => api("/api/lens/memory", "POST", { op }).catch(() => {});

async function sendRaw() {
  const r = await api("/api/raw", "POST", { hex: $("rawHex").value, checksum: $("rawCks").checked });
  $("rawReply").textContent = r.ok ? "reply: " + (r.reply || "(none)") : "error: " + r.error;
}

// ---- media library ----
let MEDIA = [];   // [{name, is_image, previewable, size_mb}]

async function loadMedia() {
  const { items } = await api("/api/media/info");
  MEDIA = items || [];
  renderGrid();
  renderSchedPicker();
}

function renderGrid() {
  const grid = $("mediaGrid");
  const prevSel = new Set(selectedFiles());   // keep ticks across re-render
  grid.innerHTML = "";
  if (!MEDIA.length) { grid.innerHTML = '<span class="hint">No media yet — upload or download something.</span>'; return; }
  MEDIA.forEach(m => {
    const cell = document.createElement("div");
    cell.className = "cell";
    cell.innerHTML = `
      <div class="thumb" title="Click to preview">
        <img loading="lazy" src="/api/media/thumb/${encodeURIComponent(m.name)}"
             onerror="this.style.display='none';this.parentNode.classList.add('nothumb')">
      </div>
      <label class="cellname"><input type="checkbox" class="pick" value="${esc(m.name)}" ${prevSel.has(m.name) ? "checked" : ""}>
        <span>${esc(m.name)}</span></label>
      <div class="cellmeta">
        <span class="hint">${m.size_mb} MB${m.is_image ? " · image" : ""}</span>
        <span class="del" title="Delete">delete</span>
      </div>`;
    cell.querySelector(".thumb").onclick = () => openPreview(m);
    cell.querySelector(".del").onclick = async () => {
      if (!confirm("Delete " + m.name + "?")) return;
      await api("/api/media/" + encodeURIComponent(m.name), "DELETE");
      loadMedia();
    };
    grid.appendChild(cell);
  });
}

function selectedFiles() {
  return Array.from($("mediaGrid").querySelectorAll(".pick:checked")).map(c => c.value);
}

// ---- preview modal ----
function openPreview(m) {
  const body = $("previewBody");
  body.innerHTML = "";
  if (m.is_image) {
    const img = document.createElement("img");
    img.src = "/api/media/preview/" + encodeURIComponent(m.name);
    body.appendChild(img);
  } else if (m.previewable) {
    const v = document.createElement("video");
    v.src = "/api/media/preview/" + encodeURIComponent(m.name);
    v.controls = true; v.autoplay = true; v.preload = "metadata";
    body.appendChild(v);
  } else {
    const img = document.createElement("img");
    img.src = "/api/media/thumb/" + encodeURIComponent(m.name);
    body.appendChild(img);
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = "In-browser preview isn't supported for this format (" +
      m.name.split(".").pop() + ") — showing a thumbnail. It still plays on the projector.";
    body.appendChild(p);
  }
  $("previewModal").classList.add("open");
}

function closePreview(e) {
  if (e && e.target.closest(".modal-box") && !e.target.classList.contains("modal-close")) return;
  $("previewModal").classList.remove("open");
  $("previewBody").innerHTML = "";   // stop/free any <video>
}

// ---- upload ----
async function upload() {
  const f = $("fileInput").files[0];
  if (!f) { $("uploadMsg").textContent = "choose a file first"; return; }
  $("uploadMsg").textContent = "uploading…";
  const fd = new FormData(); fd.append("file", f);
  const r = await (await fetch("/api/media", { method: "POST", body: fd })).json();
  $("uploadMsg").textContent = r.ok ? "uploaded " + r.name : "error: " + r.error;
  $("fileInput").value = ""; loadMedia();
}

// ---- downloads ----
async function startDownload() {
  const url = $("dlUrl").value.trim();
  if (!url) return;
  const r = await api("/api/download", "POST", { url });
  if (!r.ok) { alert("Download failed: " + r.error); return; }
  $("dlUrl").value = "";
  pollDownloads();
}
let _dlTimer = null;
const _seenDone = new Set();   // job ids we've already refreshed the library for
async function pollDownloads() {
  const { jobs } = await api("/api/downloads");
  const ul = $("dlList"); ul.innerHTML = "";
  let active = false;
  (jobs || []).slice().reverse().forEach(j => {
    if (["queued", "starting", "downloading", "processing"].includes(j.state)) active = true;
    const pct = j.percent != null ? j.percent + "%" : "";
    const extra = j.state === "downloading" && j.eta != null ? ` · ETA ${j.eta}s` : "";
    const detail = j.error ? esc(j.error) : (j.title ? esc(j.title) : esc(j.url));
    const li = document.createElement("li");
    li.innerHTML = `<span class="name"><b>${j.state}</b> ${pct}${extra}<br>
      <span class="hint">${detail}</span></span>`;
    if (["queued", "starting", "downloading", "processing"].includes(j.state)) {
      const c = document.createElement("span"); c.className = "del"; c.textContent = "cancel";
      c.onclick = () => api("/api/download/" + j.id + "/cancel", "POST");
      li.appendChild(c);
    }
    ul.appendChild(li);
    // Refresh the library exactly once per newly-finished download.
    if (j.state === "done" && !_seenDone.has(j.id)) { _seenDone.add(j.id); loadMedia(); }
  });
  if (active) { clearTimeout(_dlTimer); _dlTimer = setTimeout(pollDownloads, 1000); }
}

// ---- playback ----
$("ssNow").onchange = () => { $("ssDur").disabled = !$("ssNow").checked; };
async function playNow() {
  const files = selectedFiles();
  if (!files.length) { alert("Select one or more files."); return; }
  const body = $("ssNow").checked
    ? { source_type: "slideshow", files, loop: $("loopNow").checked, duration: parseInt($("ssDur").value) || 8 }
    : { source_type: "files", files, loop: $("loopNow").checked };
  const r = await api("/api/play", "POST", body);
  if (!r.ok) alert("Play failed: " + r.error);
}
const stopNow = () => api("/api/stop", "POST", {});
async function playRtsp() {
  const url = $("rtspUrl").value.trim();
  if (!url) return;
  const r = await api("/api/play", "POST", { source_type: "rtsp", url });
  if (!r.ok) alert("RTSP failed: " + r.error);
}

// ---- audio ----
async function loadAudio() {
  const s = await api("/api/audio/devices");
  const sel = $("audioDev"); sel.innerHTML = "";
  const opt = (v, t) => { const o = document.createElement("option"); o.value = v; o.textContent = t; sel.appendChild(o); };
  opt("auto", "Auto (default)");
  (s.devices || []).forEach(d => opt(d.name, d.description || d.name));
  sel.value = s.current || "auto";
  $("audioMute").checked = !!s.mute;
  sel.onchange = () => api("/api/audio/device", "POST", { device: sel.value });
}
const setMute = () => api("/api/audio/mute", "POST", { on: $("audioMute").checked });

// ---- schedule source-type toggle ----
function onSourceType() {
  const t = $("sType").value;
  $("sFilesWrap").style.display = (t === "files" || t === "slideshow") ? "" : "none";
  $("sSlideWrap").style.display = (t === "slideshow") ? "" : "none";
  $("sRtspWrap").style.display = (t === "rtsp") ? "" : "none";
}

// ---- reorderable schedule file picker ----
function renderSchedPicker() {
  const ul = $("schedFiles");
  // preserve current selection + order across reloads
  const prev = scheduleFiles();
  ul.innerHTML = "";
  if (!MEDIA.length) { ul.innerHTML = '<li class="hint">No files yet — add media above.</li>'; return; }
  const ordered = [...prev, ...MEDIA.map(m => m.name).filter(n => !prev.includes(n))];
  ordered.forEach(name => {
    const m = MEDIA.find(x => x.name === name);
    if (!m) return;
    const li = document.createElement("li");
    li.className = "orderitem"; li.draggable = true; li.dataset.name = name;
    li.innerHTML = `
      <span class="grip" title="Drag to reorder">⋮⋮</span>
      <input type="checkbox" class="spick" value="${esc(name)}" ${prev.includes(name) ? "checked" : ""}>
      <span class="name">${esc(name)}</span>
      <span class="ord"><button class="btn small" onclick="moveItem(this,-1)">↑</button>
        <button class="btn small" onclick="moveItem(this,1)">↓</button></span>`;
    li.addEventListener("dragstart", e => { li.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; });
    li.addEventListener("dragend", () => li.classList.remove("dragging"));
    ul.appendChild(li);
  });
  ul.ondragover = (e) => {
    e.preventDefault();
    const dragging = ul.querySelector(".dragging");
    if (!dragging) return;
    const after = [...ul.querySelectorAll(".orderitem:not(.dragging)")].find(el => {
      const r = el.getBoundingClientRect();
      return e.clientY < r.top + r.height / 2;
    });
    if (after) ul.insertBefore(dragging, after); else ul.appendChild(dragging);
  };
}
function moveItem(btn, dir) {
  const li = btn.closest(".orderitem");
  if (dir < 0 && li.previousElementSibling) li.parentNode.insertBefore(li, li.previousElementSibling);
  if (dir > 0 && li.nextElementSibling) li.parentNode.insertBefore(li.nextElementSibling, li);
}
function scheduleFiles() {
  return Array.from($("schedFiles").querySelectorAll(".orderitem"))
    .filter(li => li.querySelector(".spick") && li.querySelector(".spick").checked)
    .map(li => li.dataset.name);
}

// ---- days picker ----
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
DAYS.forEach(d => {
  const el = document.createElement("div");
  el.className = "day on"; el.textContent = d; el.dataset.day = d;
  el.onclick = () => el.classList.toggle("on");
  $("daysRow").appendChild(el);
});
const chosenDays = () => Array.from(document.querySelectorAll(".day.on")).map(d => d.dataset.day);

// ---- schedules ----
async function createSchedule() {
  const type = $("sType").value;
  if (!$("sStart").value) { alert("Set a start time."); return; }
  const body = {
    name: $("sName").value || "Untitled",
    source_type: type,
    start: $("sStart").value,
    end: $("sEnd").value || null,
    days: chosenDays(),
    loop: $("sLoop").checked,
    control_projector: $("sCtrl").checked,
    power_off_at_end: $("sOff").checked,
    input: $("sInput").value,
  };
  if (type === "rtsp") {
    body.rtsp_url = $("sRtsp").value.trim();
    if (!body.rtsp_url) { alert("Enter an RTSP URL."); return; }
  } else {
    body.files = scheduleFiles();
    if (!body.files.length) { alert("Tick one or more files (in the order you want)."); return; }
    if (type === "slideshow") { body.duration = parseInt($("sDur").value) || 8; body.shuffle = $("sShuffle").checked; }
  }
  const r = await api("/api/schedules", "POST", body);
  if (!r.ok) { alert("Create failed: " + r.error); return; }
  $("sName").value = "";
  loadSchedules();
}
async function loadSchedules() {
  const { schedules } = await api("/api/schedules");
  const ul = $("schedList"); ul.innerHTML = "";
  if (!schedules.length) { ul.innerHTML = '<li class="hint">No schedules yet.</li>'; return; }
  schedules.forEach(s => {
    const li = document.createElement("li");
    const what = s.source_type === "rtsp" ? "RTSP stream"
      : s.source_type === "slideshow" ? `slideshow · ${s.files.length} image(s) · ${s.duration}s`
      : `${s.files.length} file(s)`;
    const span = `${s.start}${s.end ? "–" + s.end : ""} · ${s.days.join(",")} · ${what}${s.loop ? " · loop" : ""}`;
    li.innerHTML = `<span class="name"><b>${esc(s.name)}</b><br><span class="hint">${esc(span)}</span></span><span class="del">delete</span>`;
    li.querySelector(".del").onclick = async () => { await api("/api/schedules/" + s.id, "DELETE"); loadSchedules(); };
    ul.appendChild(li);
  });
}

// ---- init ----
refreshStatus(); loadMedia(); loadSchedules(); loadAudio(); pollDownloads();
setInterval(refreshStatus, 5000);
