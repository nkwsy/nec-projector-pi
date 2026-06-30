"""
app.py - Web server for the NEC PX803UL controller.

Serves the control panel UI and a REST API, runs media playback on the Pi,
and fires scheduled playback jobs (with optional projector power control).
"""

import json
import os
import uuid

from flask import Flask, jsonify, request, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from werkzeug.utils import secure_filename

from nec import NECProjector, INPUTS, LENS_AXES
from player import Player

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
    "allowed_extensions": ["mp4", "mkv", "mov", "avi", "webm", "m4v",
                           "jpg", "jpeg", "png"],
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    os.makedirs(cfg["media_dir"], exist_ok=True)
    return cfg


cfg = load_config()
# Work with either layout: the tidy one (templates/ + static/) or a flat repo
# where index.html, app.js and style.css sit next to app.py.
TEMPLATE_DIR = os.path.join(BASE, "templates") if os.path.isdir(os.path.join(BASE, "templates")) else BASE
STATIC_DIR = os.path.join(BASE, "static") if os.path.isdir(os.path.join(BASE, "static")) else BASE
# static_folder=None disables Flask's built-in /static route so our own handles it.
app = Flask(__name__, static_folder=None)
pj = NECProjector(cfg["projector_ip"], cfg["projector_port"])
player = Player(cfg["media_dir"], cfg["mpv_args"])
scheduler = BackgroundScheduler()
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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
    player.play(sched["files"], loop=sched.get("loop", True))


def job_end(sched):
    player.stop()
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
    files = sorted(f for f in os.listdir(cfg["media_dir"])
                   if os.path.isfile(os.path.join(cfg["media_dir"], f)))
    return jsonify({"files": files})


@app.route("/api/media", methods=["POST"])
def api_media_upload():
    f = request.files.get("file")
    if not f or f.filename == "":
        return jsonify({"ok": False, "error": "no file"}), 400
    if not allowed(f.filename):
        return jsonify({"ok": False, "error": "type not allowed"}), 400
    name = secure_filename(f.filename)
    f.save(os.path.join(cfg["media_dir"], name))
    return jsonify({"ok": True, "name": name})


@app.route("/api/media/<name>", methods=["DELETE"])
def api_media_delete(name):
    path = os.path.join(cfg["media_dir"], secure_filename(name))
    if os.path.exists(path):
        os.remove(path)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not found"}), 404


@app.route("/api/play", methods=["POST"])
def api_play():
    d = request.json
    try:
        player.play(d["files"], loop=d.get("loop", True))
        return jsonify({"ok": True})
    except Exception as e:
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
    d = request.json
    sched = {
        "id": uuid.uuid4().hex[:8],
        "name": d.get("name", "Untitled"),
        "files": d["files"],
        "start": d["start"],                       # "HH:MM"
        "end": d.get("end"),                       # "HH:MM" or null
        "days": d.get("days") or DAYS,             # list of mon..sun
        "loop": d.get("loop", True),
        "control_projector": d.get("control_projector", False),
        "power_off_at_end": d.get("power_off_at_end", False),
        "input": d.get("input", cfg["default_input"]),
    }
    items = load_schedules()
    items.append(sched)
    save_schedules(items)
    register(sched)
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
    for s in load_schedules():
        register(s)
    scheduler.start()
    app.run(host="0.0.0.0", port=cfg["web_port"], threaded=True)


if __name__ == "__main__":
    main()
