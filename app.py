"""
app.py - Web server for the NEC PX803UL controller.

Serves the control panel UI and a REST API, runs media playback on the Pi
(local files, images/slideshow, RTSP), downloads videos via yt-dlp, generates
thumbnails/previews, and fires scheduled playback jobs (with optional projector
power control). Optionally gated behind HTTP Basic auth.
"""

import base64
import hmac
import json
import logging
import os
import shutil
import tempfile
import uuid

from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from werkzeug.utils import secure_filename

from nec import NECProjector, INPUTS, LENS_AXES
from player import Player, validate_rtsp
from downloader import Downloader, ytdlp_version
import media_util

log = logging.getLogger("projector")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
SCHEDULES_PATH = os.path.join(BASE, "schedules.json")

# ---------- config ----------
DEFAULTS = {
    "projector_ip": "192.168.0.10",
    "projector_port": 7142,
    "media_dir": os.path.join(BASE, "media"),
    "mpv_args": None,
    "web_port": 8080,
    "default_input": "HDMI",
    "max_upload_mb": 4096,
    "allowed_extensions": ["mp4", "mkv", "mov", "avi", "webm", "m4v",
                           "jpg", "jpeg", "png", "gif", "webp"],
    # audio
    "audio_device": "auto",
    "audio_mute": False,
    # downloads
    "max_download_height": 1080,
    "download_container": "mkv",
    "download_allowed_hosts": None,       # None -> module default allowlist
    "download_cookiefile": None,
    # auth (empty password disables the gate; set one to protect the panel)
    "auth_user": "admin",
    "auth_password": "",
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    os.makedirs(cfg["media_dir"], exist_ok=True)
    return cfg


cfg = load_config()

# Large uploads are buffered to a temp file before being saved. The default temp
# dir (/tmp) is often a small RAM-backed tmpfs on a Pi, so a big video overflows
# it and fails with "No space left on device" even when the SD card is nearly
# empty. Point the temp dir at the same (roomy) filesystem as media_dir instead.
UPLOAD_TMP = os.path.join(cfg["media_dir"], ".uploadtmp")
os.makedirs(UPLOAD_TMP, exist_ok=True)
tempfile.tempdir = UPLOAD_TMP
os.environ["TMPDIR"] = UPLOAD_TMP

# Work with either layout: the tidy one (templates/ + static/) or a flat repo
# where index.html, app.js and style.css sit next to app.py.
TEMPLATE_DIR = os.path.join(BASE, "templates") if os.path.isdir(os.path.join(BASE, "templates")) else BASE
STATIC_DIR = os.path.join(BASE, "static") if os.path.isdir(os.path.join(BASE, "static")) else BASE
# static_folder=None disables Flask's built-in /static route so our own handles it.
app = Flask(__name__, static_folder=None)
# Reject oversized uploads before they're buffered to disk.
app.config["MAX_CONTENT_LENGTH"] = int(cfg["max_upload_mb"]) * 1024 * 1024

pj = NECProjector(cfg["projector_ip"], cfg["projector_port"])
player = Player(cfg["media_dir"], cfg["mpv_args"],
                audio_device=cfg.get("audio_device", "auto"),
                mute=cfg.get("audio_mute", False))
dl = Downloader(cfg["media_dir"],
                max_height=cfg.get("max_download_height", 1080),
                merge_format=cfg.get("download_container", "mkv"),
                allowed_hosts=cfg.get("download_allowed_hosts"),
                cookiefile=cfg.get("download_cookiefile"))
# misfire_grace_time: still fire a start/end job that's a few minutes late (a busy
# Pi, a clock jump, or the 35s power-on sleep can delay a worker) instead of
# silently dropping it. coalesce: collapse a backlog into one run.
scheduler = BackgroundScheduler(
    job_defaults={"misfire_grace_time": 300, "coalesce": True})
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _valid_hhmm(s):
    if not isinstance(s, str):
        return False
    parts = s.split(":")
    if len(parts) != 2:
        return False
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= h <= 23 and 0 <= m <= 59

# ---------- auth ----------
AUTH_USER = cfg.get("auth_user", "admin")
AUTH_PASS = cfg.get("auth_password") or ""
if not AUTH_PASS:
    log.warning("No auth_password set in config.json — the panel is UNPROTECTED. "
                "Anyone on the network can control the projector. Set one, and "
                "keep port %s off the public internet.", cfg["web_port"])


@app.before_request
def _require_auth():
    if not AUTH_PASS:
        return None
    hdr = request.headers.get("Authorization", "")
    if hdr.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
        except Exception:
            user = pw = ""
        if (hmac.compare_digest(user, AUTH_USER)
                and hmac.compare_digest(pw, AUTH_PASS)):
            return None
    return Response("Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="Projector Controller"'})


# ---------- schedule persistence ----------
def load_schedules():
    if os.path.exists(SCHEDULES_PATH):
        with open(SCHEDULES_PATH) as f:
            return json.load(f)
    return []


def save_schedules(items):
    with open(SCHEDULES_PATH, "w") as f:
        json.dump(items, f, indent=2)


# ---------- schedule actions (run by the scheduler) ----------
def _start_source(source_type, params, owner=None):
    """Dispatch a playback request by source type (shared by /api/play + jobs)."""
    if source_type == "rtsp":
        player.play_rtsp(params["url"], owner=owner)
    elif source_type == "slideshow":
        player.play_slideshow(params["files"],
                              duration=params.get("duration", 8),
                              loop=params.get("loop", True),
                              shuffle=params.get("shuffle", False),
                              owner=owner)
    else:
        player.play(params["files"], loop=params.get("loop", True), owner=owner)


def job_start(sched):
    if sched.get("control_projector"):
        pj.power_on()
        # The projector ignores commands while powering on; give it a moment,
        # then open the shutter and select the input.
        import time
        time.sleep(35)
        pj.shutter_open()
        inp = sched.get("input") or cfg["default_input"]
        if inp in INPUTS:
            pj.input_select_name(inp)
    _start_source(sched.get("source_type", "files"), {
        "files": sched.get("files", []),
        "url": sched.get("rtsp_url"),
        "loop": sched.get("loop", True),
        "duration": sched.get("duration", 8),
        "shuffle": sched.get("shuffle", False),
    }, owner=sched["id"])


def job_end(sched):
    # Only stop if this schedule is what's actually playing — don't tear down a
    # manual playback or an overlapping schedule that's still meant to run.
    player.stop_owned(sched["id"])
    if sched.get("control_projector"):
        pj.shutter_close()
        if sched.get("power_off_at_end"):
            pj.power_off()


def _cron_kwargs(sched, time_str):
    hh, mm = time_str.split(":")
    days = sched.get("days") or DAYS
    return {"day_of_week": ",".join(days), "hour": int(hh), "minute": int(mm)}


def register(sched):
    scheduler.add_job(job_start, CronTrigger(**_cron_kwargs(sched, sched["start"])),
                      id=f"{sched['id']}_start", args=[sched], replace_existing=True)
    if sched.get("end"):
        scheduler.add_job(job_end, CronTrigger(**_cron_kwargs(sched, sched["end"])),
                          id=f"{sched['id']}_end", args=[sched], replace_existing=True)


def unregister(sid):
    for suffix in ("_start", "_end"):
        try:
            scheduler.remove_job(sid + suffix)
        except Exception:
            pass


# ---------- web routes ----------
@app.route("/")
def index():
    return send_from_directory(TEMPLATE_DIR, "index.html")


@app.route("/api/status")
def api_status():
    st = pj.status()
    st["playback"] = player.status()
    st["inputs"] = list(INPUTS.keys())
    st["axes"] = list(LENS_AXES.keys())
    st["features"] = {
        "ytdlp": ytdlp_version(),
        "ffmpeg": media_util.ffmpeg_available(),
    }
    return jsonify(st)


@app.route("/api/power", methods=["POST"])
def api_power():
    on = request.json.get("on")
    ok = pj.power_on() if on else pj.power_off()
    return jsonify({"ok": ok})


@app.route("/api/lens", methods=["POST"])
def api_lens():
    d = request.json
    axis, action = d["axis"], d["action"]
    if action == "jog":
        ok = pj.lens_jog(axis, d["direction"], float(d.get("seconds", 0.5)))
    elif action == "start":
        ok = pj.lens_continuous(axis, d["direction"])
    elif action == "stop":
        ok = pj.lens_stop(axis)
    elif action == "stop_all":
        ok = pj.lens_stop_all()
    else:
        return jsonify({"ok": False, "error": "bad action"}), 400
    return jsonify({"ok": ok})


@app.route("/api/lens/position/<axis>")
def api_lens_pos(axis):
    return jsonify(pj.get_lens_position(axis) or {})


@app.route("/api/lens/memory", methods=["POST"])
def api_lens_memory():
    op = request.json.get("op")
    if op == "store":
        return jsonify({"ok": pj.lens_memory_store()})
    return jsonify({"ok": pj.lens_memory_move()})


@app.route("/api/shutter", methods=["POST"])
def api_shutter():
    return jsonify({"ok": pj.shutter_open() if request.json.get("open") else pj.shutter_close()})


@app.route("/api/mute", methods=["POST"])
def api_mute():
    return jsonify({"ok": pj.picture_mute_on() if request.json.get("on") else pj.picture_mute_off()})


@app.route("/api/freeze", methods=["POST"])
def api_freeze():
    return jsonify({"ok": pj.freeze_on() if request.json.get("on") else pj.freeze_off()})


@app.route("/api/input", methods=["POST"])
def api_input():
    return jsonify({"ok": pj.input_select_name(request.json["input"])})


@app.route("/api/raw", methods=["POST"])
def api_raw():
    d = request.json
    try:
        reply = pj.send_raw(d["hex"], append_checksum=d.get("checksum", False))
        return jsonify({"ok": True, "reply": reply})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# ---------- media ----------
def allowed(name):
    return "." in name and name.rsplit(".", 1)[1].lower() in cfg["allowed_extensions"]


@app.route("/api/media")
def api_media_list():
    """Bare filename list (kept for backward compatibility)."""
    files = sorted(f for f in os.listdir(cfg["media_dir"])
                   if os.path.isfile(os.path.join(cfg["media_dir"], f)))
    return jsonify({"files": files})


@app.route("/api/media/info")
def api_media_info():
    """Richer list: size + whether each item is an image / browser-previewable."""
    items = []
    for f in sorted(os.listdir(cfg["media_dir"])):
        p = os.path.join(cfg["media_dir"], f)
        if not os.path.isfile(p):
            continue
        items.append({
            "name": f,
            "size_mb": round(os.path.getsize(p) / 1048576, 1),
            "is_image": media_util.is_image(f),
            "previewable": media_util.previewable(f),
        })
    return jsonify({"items": items})


@app.route("/api/media/thumb/<path:name>")
def api_media_thumb(name):
    t = media_util.ensure_thumb(cfg["media_dir"], name)
    if not t:
        return ("", 404)
    return send_file(t, mimetype="image/jpeg", conditional=True)


_PREVIEW_MIME = {
    "mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm",
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp",
}


@app.route("/api/media/preview/<path:name>")
def api_media_preview(name):
    p = media_util.safe_media_path(cfg["media_dir"], name)
    if not p:
        return ("", 404)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mt = _PREVIEW_MIME.get(ext, "application/octet-stream")
    # conditional=True gives us HTTP Range (206) support so browsers can seek.
    return send_file(p, mimetype=mt, conditional=True)


@app.route("/api/disk")
def api_disk():
    total, used, free = shutil.disk_usage(cfg["media_dir"])
    return jsonify({
        "free_mb": round(free / 1048576),
        "total_mb": round(total / 1048576),
        "used_pct": round(used / total * 100),
    })


@app.errorhandler(413)
def too_large(_):
    return jsonify({"ok": False, "error": f"File exceeds max upload size "
                    f"({cfg['max_upload_mb']} MB)"}), 413


@app.route("/api/media", methods=["POST"])
def api_media_upload():
    try:
        f = request.files.get("file")
    except OSError as e:
        # Errno 28 = no space left on device while buffering the upload.
        _, _, free = shutil.disk_usage(cfg["media_dir"])
        return jsonify({"ok": False, "error": f"Disk write failed ({e.strerror}). "
                        f"Only {round(free/1048576)} MB free — free up space or "
                        f"move media_dir to a USB drive."}), 507
    if not f or f.filename == "":
        return jsonify({"ok": False, "error": "no file"}), 400
    if not allowed(f.filename):
        return jsonify({"ok": False, "error": "type not allowed"}), 400
    name = secure_filename(f.filename)
    if not name:
        return jsonify({"ok": False, "error": "invalid filename"}), 400
    dest = os.path.join(cfg["media_dir"], name)
    try:
        f.save(dest)
    except OSError as e:
        if os.path.exists(dest):
            os.remove(dest)   # remove the partial file
        _, _, free = shutil.disk_usage(cfg["media_dir"])
        return jsonify({"ok": False, "error": f"Save failed ({e.strerror}). "
                        f"Only {round(free/1048576)} MB free."}), 507
    media_util.ensure_thumb_async(cfg["media_dir"], name)
    return jsonify({"ok": True, "name": name})


@app.route("/api/media/<name>", methods=["DELETE"])
def api_media_delete(name):
    path = media_util.safe_media_path(cfg["media_dir"], name)
    if path:
        os.remove(path)
        media_util.remove_thumb(cfg["media_dir"], name)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not found"}), 404


# ---------- downloads ----------
@app.route("/api/download", methods=["POST"])
def api_download():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "no url"}), 400
    _, _, free = shutil.disk_usage(cfg["media_dir"])
    if free < 500 * 1048576:
        return jsonify({"ok": False, "error": "Low disk space"}), 507
    try:
        jid = dl.start(url)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 501
    return jsonify({"ok": True, "id": jid})


@app.route("/api/downloads")
def api_downloads():
    return jsonify({"jobs": dl.status()})


@app.route("/api/download/<jid>")
def api_download_status(jid):
    st = dl.status(jid)
    return (jsonify(st), 200) if st else (jsonify({"error": "not found"}), 404)


@app.route("/api/download/<jid>/cancel", methods=["POST"])
def api_download_cancel(jid):
    return jsonify({"ok": dl.cancel(jid)})


# ---------- audio ----------
@app.route("/api/audio/devices")
def api_audio_devices():
    return jsonify({"devices": player.list_audio_devices(),
                    "current": player.audio_device, "mute": player.mute})


@app.route("/api/audio/device", methods=["POST"])
def api_audio_device():
    dev = (request.json or {}).get("device", "auto")
    devs = player.list_audio_devices()
    # Only reject against a non-empty list; an empty list means the mpv IPC query
    # failed/timed out (e.g. mid RTSP restart), not that the device is invalid.
    if devs:
        valid = {"auto"} | {d.get("name") for d in devs}
        if dev not in valid:
            return jsonify({"ok": False, "error": "unknown device"}), 400
    return jsonify({"ok": player.set_audio_device(dev), "device": dev})


@app.route("/api/audio/mute", methods=["POST"])
def api_audio_mute():
    on = bool((request.json or {}).get("on"))
    return jsonify({"ok": player.set_mute(on), "mute": on})


# ---------- playback ----------
@app.route("/api/play", methods=["POST"])
def api_play():
    d = request.json or {}
    st = d.get("source_type", "files")
    try:
        _start_source(st, {
            "files": d.get("files", []),
            "url": d.get("url", ""),
            "loop": d.get("loop", True),
            "duration": d.get("duration", 8),
            "shuffle": d.get("shuffle", False),
        })
        return jsonify({"ok": True})
    except (ValueError, FileNotFoundError, KeyError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/stop", methods=["POST"])
def api_stop():
    return jsonify({"ok": player.stop()})


# ---------- schedules ----------
@app.route("/api/schedules")
def api_sched_list():
    return jsonify({"schedules": load_schedules()})


@app.route("/api/schedules", methods=["POST"])
def api_sched_create():
    d = request.json or {}
    source_type = d.get("source_type", "files")
    rtsp_url = d.get("rtsp_url")
    # Validate BEFORE persisting — an unchecked start/days used to be written to
    # schedules.json even when register() then threw, permanently crash-looping
    # the service on the next boot (it re-registers every saved schedule).
    start = d.get("start")
    end = d.get("end") or None
    days = d.get("days") or DAYS
    if not _valid_hhmm(start):
        return jsonify({"ok": False, "error": "Invalid start time (use HH:MM)"}), 400
    if end is not None and not _valid_hhmm(end):
        return jsonify({"ok": False, "error": "Invalid end time (use HH:MM)"}), 400
    if not (isinstance(days, list) and days and all(x in DAYS for x in days)):
        return jsonify({"ok": False, "error": "Invalid days"}), 400
    if source_type == "rtsp":
        try:
            rtsp_url = validate_rtsp(rtsp_url)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    elif not d.get("files"):
        return jsonify({"ok": False, "error": "Select at least one file"}), 400
    sched = {
        "id": uuid.uuid4().hex[:8],
        "name": d.get("name", "Untitled"),
        "source_type": source_type,                # files | slideshow | rtsp
        "files": d.get("files", []),               # ordered list (files/slideshow)
        "rtsp_url": rtsp_url,                       # for rtsp
        "duration": d.get("duration", 8),          # per-image seconds (slideshow)
        "shuffle": d.get("shuffle", False),        # slideshow
        "start": start,                            # "HH:MM"
        "end": end,                                # "HH:MM" or null
        "days": days,                              # list of mon..sun
        "loop": d.get("loop", True),
        "control_projector": d.get("control_projector", False),
        "power_off_at_end": d.get("power_off_at_end", False),
        "input": d.get("input", cfg["default_input"]),
    }
    try:
        register(sched)                            # validated above, shouldn't throw
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not schedule: {e}"}), 400
    items = load_schedules()
    items.append(sched)
    save_schedules(items)
    return jsonify({"ok": True, "schedule": sched})


@app.route("/api/schedules/<sid>", methods=["DELETE"])
def api_sched_delete(sid):
    items = [s for s in load_schedules() if s["id"] != sid]
    save_schedules(items)
    unregister(sid)
    return jsonify({"ok": True})


@app.route("/static/<path:p>")
def static_files(p):
    return send_from_directory(STATIC_DIR, p)


def main():
    # Bring mpv up first so the projector shows solid black immediately, before
    # network/scheduler come up (this is the "elegant boot" behaviour).
    try:
        player.ensure_running()
    except Exception as e:
        log.warning("Could not start mpv holder at boot: %s", e)
    for s in load_schedules():
        # Never let one bad persisted schedule crash-loop the whole service.
        try:
            register(s)
        except Exception as e:
            log.warning("Skipping invalid schedule %s: %s", s.get("id"), e)
    scheduler.start()
    app.run(host="0.0.0.0", port=cfg["web_port"], threaded=True)


if __name__ == "__main__":
    main()
