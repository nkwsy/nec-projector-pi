"""
player.py - Media playback on the Raspberry Pi via mpv.

The Pi's HDMI output is fed into the projector's HDMI IN. mpv renders fullscreen.

Design: ONE long-lived mpv process (the "holder") is started at boot with
--idle --force-window, so it grabs the DRM/KMS display immediately and holds a
solid black frame. Content is swapped in over mpv's JSON IPC socket with
`loadfile` — no process relaunch, no HDMI re-sync flash between clips, and audio
device / mute can be changed live. This is what makes the boot elegant (black
from the moment KMS is up) and audio switching instant.

RTSP is the exception: it needs demuxer/cache options best set at launch and its
own reconnect loop, so it runs as a dedicated mpv (the holder is replaced while
streaming, then restored to idle-black on stop). Only ever one mpv → one DRM
master, so the two never fight over the display.
"""

import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time

from media_util import safe_media_path

# rtsp:// or rtsps://, no control characters.
_RTSP_RE = re.compile(r"^rtsps?://[^\s\x00-\x1f]+$", re.IGNORECASE)


def validate_rtsp(url):
    url = (url or "").strip()
    if not _RTSP_RE.match(url):
        raise ValueError("Invalid RTSP URL (must start with rtsp:// or rtsps://)")
    return url


def _redact(url):
    """Hide user:pass@ credentials before showing/logging an RTSP URL."""
    return re.sub(r"//[^/@]*@", "//***@", url) if url else url


def _runtime_socket():
    """A private path for mpv's IPC socket. Prefer /run/projector (created 0700
    by the systemd unit's RuntimeDirectory=); fall back to a temp dir for dev."""
    for d in ("/run/projector", os.path.join(tempfile.gettempdir(), "projector")):
        try:
            os.makedirs(d, exist_ok=True)
            os.chmod(d, 0o700)
            return os.path.join(d, "mpv.sock")
        except OSError:
            continue
    return os.path.join(tempfile.gettempdir(), "mpv-projector.sock")


class Player:
    def __init__(self, media_dir, mpv_args=None, audio_device="auto", mute=False):
        self.media_dir = media_dir
        # Rendering args from config (DRM/KMS on headless, or desktop defaults).
        self.mpv_args = mpv_args or ["--vo=gpu", "--gpu-context=drm"]
        self.sock = _runtime_socket()

        self.proc = None
        self._kind = None                # "holder" (idle/files/slideshow) | "rtsp"
        self.source_type = "idle"        # idle | files | slideshow | rtsp
        self.current = []                # absolute paths currently loaded
        self._rtsp_url = None
        self._owner = None               # schedule id that started current playback (None = manual)

        self.audio_device = audio_device or "auto"
        self.mute = bool(mute)

        self._lock = threading.RLock()
        self._gen = 0                    # bumps on every mode change; kills stale supervisors
        self._wd = None

    # --- display env -------------------------------------------------------
    def _display_env(self):
        """Environment mpv needs to reach the screen. Headless DRM ignores these;
        a desktop session needs WAYLAND_DISPLAY/DISPLAY, which we auto-detect."""
        env = os.environ.copy()
        xdg = env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
        env.setdefault("XDG_RUNTIME_DIR", xdg)
        if "WAYLAND_DISPLAY" not in env:
            try:
                socks = sorted(n for n in os.listdir(xdg)
                               if n.startswith("wayland-") and not n.endswith(".lock"))
            except OSError:
                socks = []
            if socks:
                env["WAYLAND_DISPLAY"] = socks[0]
        env.setdefault("DISPLAY", ":0")
        return env

    # --- ipc ---------------------------------------------------------------
    def _ipc(self, command, timeout=2.0):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(self.sock)
        except OSError:
            return None
        try:
            f = s.makefile("rwb")
            f.write((json.dumps({"command": command, "request_id": 1}) + "\n").encode())
            f.flush()
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    line = f.readline()
                except (socket.timeout, OSError):
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line.decode(errors="ignore"))
                except ValueError:
                    continue
                if msg.get("request_id") == 1:
                    return msg          # {"error": "success", "data": ...}
            return None
        finally:
            try:
                s.close()
            except OSError:
                pass

    def _cmd(self, *args):
        return self._ipc(list(args))

    def _get(self, prop):
        r = self._ipc(["get_property", prop])
        return r.get("data") if r and r.get("error") == "success" else None

    def _set(self, prop, val):
        r = self._ipc(["set_property", prop, val])
        return bool(r and r.get("error") == "success")

    # --- process lifecycle -------------------------------------------------
    def _kill(self):
        if self.proc and self.proc.poll() is None:
            self._ipc(["quit"], timeout=0.5)
            time.sleep(0.1)
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self.proc = None
        self._kind = None

    def _launch(self, extra_args, kind):
        self._kill()
        if os.path.exists(self.sock):
            try:
                os.remove(self.sock)
            except OSError:
                pass
        args = (["mpv", f"--input-ipc-server={self.sock}",
                 "--no-terminal", "--no-osc", "--fullscreen",
                 "--no-input-default-bindings", "--cursor-autohide=always"]
                + list(self.mpv_args) + extra_args)
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                     env=self._display_env())
        self._kind = kind
        self._wait_socket()

    def _wait_socket(self, timeout=6.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                return False
            if self._ipc(["get_property", "mpv-version"], timeout=0.5) is not None:
                return True
            time.sleep(0.1)
        return False

    def _holder_extra(self):
        return ["--idle=yes", "--force-window=yes", "--hwdec=auto-safe",
                "--image-display-duration=10", "--loop-playlist=no"]

    def _ensure_holder(self):
        if self.proc and self.proc.poll() is None and self._kind == "holder":
            return
        self._launch(self._holder_extra(), "holder")
        self._apply_audio()

    def ensure_running(self):
        """Bring up the idle-black holder (call at boot) and start the watchdog."""
        with self._lock:
            self._ensure_holder()
        self._start_watchdog()

    def _start_watchdog(self):
        if self._wd:
            return

        def run():
            while True:
                time.sleep(5)
                with self._lock:
                    if self._kind == "holder" and (
                            not self.proc or self.proc.poll() is not None):
                        try:
                            self._ensure_holder()
                        except Exception:
                            pass
        self._wd = threading.Thread(target=run, daemon=True)
        self._wd.start()

    # --- helpers -----------------------------------------------------------
    def _resolve(self, files):
        out = []
        for f in files or []:
            p = safe_media_path(self.media_dir, f)
            if p:
                out.append(p)
        return out

    def _load_playlist(self, paths):
        self._cmd("loadfile", paths[0], "replace")
        for p in paths[1:]:
            self._cmd("loadfile", p, "append")

    def _apply_audio(self):
        self._set("audio-device", self.audio_device or "auto")
        self._set("mute", self.mute)

    def _rtsp_extra(self, url):
        return ["--profile=low-latency", "--rtsp-transport=tcp",
                "--network-timeout=15", "--cache=no", "--hwdec=auto-safe",
                "--loop-playlist=no", "--keep-open=no", url]

    # --- playback ----------------------------------------------------------
    def play(self, files, loop=True, owner=None):
        """Play a list of media files (names in the library) as a playlist."""
        paths = self._resolve(files)
        if not paths:
            raise FileNotFoundError("No valid media files to play")
        with self._lock:
            self._gen += 1
            self._ensure_holder()
            self._set("image-display-duration", 10)
            self._set("loop-playlist", "inf" if loop else "no")
            self._load_playlist(paths)
            self.source_type = "files"
            self.current = paths
            self._rtsp_url = None
            self._owner = owner
        return True

    def play_slideshow(self, files, duration=8, loop=True, shuffle=False, owner=None):
        paths = self._resolve(files)
        if not paths:
            raise FileNotFoundError("No valid media files to play")
        try:
            duration = max(1.0, float(duration))
        except (TypeError, ValueError):
            duration = 8.0
        with self._lock:
            self._gen += 1
            self._ensure_holder()
            self._set("image-display-duration", duration)
            self._set("loop-playlist", "inf" if loop else "no")
            self._load_playlist(paths)
            if shuffle:
                self._cmd("playlist-shuffle")
            self.source_type = "slideshow"
            self.current = paths
            self._rtsp_url = None
            self._owner = owner
        return True

    def play_rtsp(self, url, owner=None):
        url = validate_rtsp(url)
        with self._lock:
            self._gen += 1
            mygen = self._gen
            self._launch(self._rtsp_extra(url), "rtsp")
            self._apply_audio()
            self.source_type = "rtsp"
            self._rtsp_url = url
            self.current = []
            self._owner = owner
            self._spawn_rtsp_supervisor(url, mygen)
        return True

    def _spawn_rtsp_supervisor(self, url, mygen):
        """mpv/FFmpeg don't auto-reconnect RTSP, so when the dedicated mpv exits
        (stream dropped) we relaunch it, until the mode changes (gen bump)."""
        def run():
            while self._gen == mygen:
                p = self.proc
                if p is not None:
                    try:
                        p.wait()
                    except Exception:
                        pass
                if self._gen != mygen:
                    return
                # Backoff, bailing immediately if the mode changed.
                for _ in range(20):
                    if self._gen != mygen:
                        return
                    time.sleep(0.1)
                with self._lock:
                    if self._gen != mygen:
                        return
                    try:
                        self._launch(self._rtsp_extra(url), "rtsp")
                        self._apply_audio()
                    except Exception:
                        # transient (device busy, etc.) — loop retries after backoff
                        pass
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        """Return to idle-black. Keeps the holder alive (no display re-sync);
        for RTSP the dedicated mpv is replaced by a fresh idle holder."""
        with self._lock:
            self._gen += 1               # invalidate any rtsp supervisor
            if self._kind == "rtsp":
                self._ensure_holder()    # replaces rtsp mpv with idle-black
            else:
                self._cmd("playlist-clear")
                self._cmd("stop")        # clears current file -> idle black
            self.source_type = "idle"
            self.current = []
            self._rtsp_url = None
            self._owner = None
        return True

    def stop_owned(self, owner):
        """Stop only if the current playback was started by `owner` (a schedule
        id). Protects manual playback and other schedules from being torn down
        by an unrelated schedule's end time."""
        with self._lock:
            if self._owner != owner:
                return False
        return self.stop()

    # --- audio -------------------------------------------------------------
    def list_audio_devices(self):
        """[{name, description}] from the running mpv; empty if none/unavailable."""
        data = self._get("audio-device-list")
        return data if isinstance(data, list) else []

    def set_audio_device(self, dev):
        with self._lock:
            self.audio_device = dev or "auto"
            return self._set("audio-device", self.audio_device)

    def set_mute(self, on):
        with self._lock:
            self.mute = bool(on)
            return self._set("mute", self.mute)

    # --- status ------------------------------------------------------------
    def is_playing(self):
        p = self.proc                    # snapshot: a concurrent _kill() may null it
        if not (p and p.poll() is None):
            return False
        if self._kind == "rtsp":
            return True
        return self._get("idle-active") is False

    def status(self):
        st = {
            "playing": self.is_playing(),
            "source_type": self.source_type,
            "audio_device": self.audio_device,
            "mute": self.mute,
            "files": [os.path.basename(p) for p in self.current],
        }
        if self.source_type == "rtsp":
            st["rtsp"] = _redact(self._rtsp_url)
        return st
