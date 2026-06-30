"""
player.py - Media playback on the Raspberry Pi via mpv.

The Pi's HDMI output is fed into the projector's HDMI IN. mpv renders fullscreen.
We launch one mpv process per playback request and control/stop it via its
JSON IPC socket. This keeps playback robust: if the web app restarts, a stray
mpv can still be found and stopped through the socket.
"""

import json
import os
import socket
import subprocess
import time

IPC_SOCKET = "/tmp/mpv-projector.sock"


class Player:
    def __init__(self, media_dir, mpv_args=None):
        self.media_dir = media_dir
        # Default args suit a Raspberry Pi. Override in config.json for your setup:
        #   Desktop (X / Wayland): the defaults work as-is.
        #   Headless console (no desktop): add "--vo=gpu", "--gpu-context=drm"
        self.mpv_args = mpv_args or [
            "--fullscreen",
            "--no-terminal",
            "--no-osc",
            "--image-display-duration=10",
            "--keep-open=no",
        ]
        self.proc = None
        self.current = []

    # --- ipc ---------------------------------------------------------------
    def _ipc(self, command):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                s.connect(IPC_SOCKET)
                s.sendall((json.dumps({"command": command}) + "\n").encode())
                return s.recv(4096).decode(errors="ignore")
        except (OSError, socket.timeout):
            return None

    # --- control -----------------------------------------------------------
    def play(self, files, loop=True):
        """Play a list of media files (absolute paths or names under media_dir)."""
        self.stop()
        paths = []
        for f in files:
            p = f if os.path.isabs(f) else os.path.join(self.media_dir, f)
            if os.path.exists(p):
                paths.append(p)
        if not paths:
            raise FileNotFoundError("No valid media files to play")

        if os.path.exists(IPC_SOCKET):
            try:
                os.remove(IPC_SOCKET)
            except OSError:
                pass

        args = ["mpv", f"--input-ipc-server={IPC_SOCKET}"] + list(self.mpv_args)
        if loop:
            args.append("--loop-playlist=inf")
        args += paths

        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL)
        self.current = paths
        return True

    def stop(self):
        # Try a graceful quit over IPC first, then terminate the process.
        self._ipc(["quit"])
        if self.proc and self.proc.poll() is None:
            time.sleep(0.2)
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self.proc = None
        self.current = []
        return True

    def is_playing(self):
        return self.proc is not None and self.proc.poll() is None

    def status(self):
        return {
            "playing": self.is_playing(),
            "files": [os.path.basename(p) for p in self.current],
        }
