const $ = (id) => document.getElementById(id);
const api = async (path, method = "GET", body = null) => {
  const opt = { method, headers: {} };
  if (body) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  return r.json();
};

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
      const play = s.playback.playing ? " · playing" : "";
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

// ---- media ----
async function loadMedia() {
  const { files } = await api("/api/media");
  const ul = $("mediaList"); ul.innerHTML = "";
  files.forEach(f => {
    const li = document.createElement("li");
    li.innerHTML = `<input type="checkbox" class="pick" value="${f}">
      <span class="name">${f}</span><span class="del">delete</span>`;
    li.querySelector(".del").onclick = async () => { await api("/api/media/" + encodeURIComponent(f), "DELETE"); loadMedia(); };
    ul.appendChild(li);
  });
}
function selectedFiles() {
  return Array.from(document.querySelectorAll(".pick:checked")).map(c => c.value);
}
async function upload() {
  const f = $("fileInput").files[0];
  if (!f) { $("uploadMsg").textContent = "choose a file first"; return; }
  $("uploadMsg").textContent = "uploading…";
  const fd = new FormData(); fd.append("file", f);
  const r = await (await fetch("/api/media", { method: "POST", body: fd })).json();
  $("uploadMsg").textContent = r.ok ? "uploaded " + r.name : "error: " + r.error;
  $("fileInput").value = ""; loadMedia();
}
async function playNow() {
  const files = selectedFiles();
  if (!files.length) { alert("Select one or more files."); return; }
  await api("/api/play", "POST", { files, loop: $("loopNow").checked });
}
const stopNow = () => api("/api/stop", "POST", {});

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
  const files = selectedFiles();
  if (!files.length) { alert("Select files in the library list first."); return; }
  if (!$("sStart").value) { alert("Set a start time."); return; }
  const body = {
    name: $("sName").value || "Untitled",
    files,
    start: $("sStart").value,
    end: $("sEnd").value || null,
    days: chosenDays(),
    loop: $("sLoop").checked,
    control_projector: $("sCtrl").checked,
    power_off_at_end: $("sOff").checked,
    input: $("sInput").value,
  };
  await api("/api/schedules", "POST", body);
  $("sName").value = "";
  loadSchedules();
}
async function loadSchedules() {
  const { schedules } = await api("/api/schedules");
  const ul = $("schedList"); ul.innerHTML = "";
  if (!schedules.length) { ul.innerHTML = '<li class="hint">No schedules yet.</li>'; return; }
  schedules.forEach(s => {
    const li = document.createElement("li");
    const span = `${s.start}${s.end ? "–" + s.end : ""} · ${s.days.join(",")} · ${s.files.length} file(s)${s.loop ? " · loop" : ""}`;
    li.innerHTML = `<span class="name"><b>${s.name}</b><br><span class="hint">${span}</span></span><span class="del">delete</span>`;
    li.querySelector(".del").onclick = async () => { await api("/api/schedules/" + s.id, "DELETE"); loadSchedules(); };
    ul.appendChild(li);
  });
}

// ---- init ----
refreshStatus(); loadMedia(); loadSchedules();
setInterval(refreshStatus, 5000);
