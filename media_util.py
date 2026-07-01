"""
media_util.py - Media helpers: safe path resolution, thumbnails, probing.

Kept separate from playback (player.py) and download (downloader.py) so the
same path-safety and thumbnail logic is shared by every route that touches a
file on disk. Thumbnails are a browser-only concern and never touch mpv/DRM.
"""

import os
import shutil
import subprocess
import threading

# Image formats we treat as pictures (slideshow + inline browser preview).
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}
# Video containers browsers can reliably play inline in a <video> tag. mkv/avi/
# mov generally can't be previewed in-browser, so they fall back to a thumbnail.
PREVIEWABLE_VIDEO = {"mp4", "m4v", "webm"}

THUMBS_DIRNAME = ".thumbs"

_thumb_locks = {}
_locks_guard = threading.Lock()


def _ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_image(name):
    return _ext(name) in IMAGE_EXTS


def previewable(name):
    e = _ext(name)
    return e in IMAGE_EXTS or e in PREVIEWABLE_VIDEO


def safe_media_path(media_dir, name):
    """Resolve `name` to a real file directly inside media_dir, else None.

    The library is a flat directory, so we deliberately reduce `name` to its
    basename and confirm the resolved path stays under media_dir. This blocks
    path traversal ('../../etc/passwd') and absolute-path escapes from any
    caller (play, preview, thumb, delete all route through here).
    """
    if not name:
        return None
    base = os.path.realpath(media_dir)
    cand = os.path.realpath(os.path.join(base, os.path.basename(name)))
    try:
        if os.path.commonpath([cand, base]) != base:
            return None
    except ValueError:
        return None
    return cand if os.path.isfile(cand) else None


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def _lock_for(key):
    with _locks_guard:
        lock = _thumb_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _thumb_locks[key] = lock
        return lock


def thumb_path(media_dir, name):
    return os.path.join(media_dir, THUMBS_DIRNAME, os.path.basename(name) + ".jpg")


def _probe_duration(path):
    """Seconds via ffprobe, or None if unavailable."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def ensure_thumb(media_dir, name):
    """Return a cached 320px-wide JPEG thumbnail for `name`, generating it with
    ffmpeg if missing/stale. Returns the path, or None on any failure (e.g. no
    ffmpeg). Safe to call concurrently — one ffmpeg run per file at a time.
    """
    src = safe_media_path(media_dir, name)
    if not src or not ffmpeg_available():
        return None
    tpath = thumb_path(media_dir, name)
    try:
        if os.path.exists(tpath) and os.path.getmtime(tpath) >= os.path.getmtime(src):
            return tpath
    except OSError:
        pass

    with _lock_for(tpath):
        # Re-check inside the lock: another thread may have just built it.
        try:
            if os.path.exists(tpath) and os.path.getmtime(tpath) >= os.path.getmtime(src):
                return tpath
        except OSError:
            pass
        os.makedirs(os.path.dirname(tpath), exist_ok=True)
        tmp = tpath + ".tmp.jpg"
        vf = "scale=320:-2:force_original_aspect_ratio=decrease"
        if is_image(name):
            cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf,
                   "-frames:v", "1", "-q:v", "4", tmp]
        else:
            # Seek a few seconds in for a representative frame, but not past the
            # end of a very short clip.
            dur = _probe_duration(src)
            ss = "3" if (dur is None or dur > 4) else "0"
            cmd = ["ffmpeg", "-y", "-ss", ss, "-i", src, "-frames:v", "1",
                   "-vf", vf, "-q:v", "4", tmp]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=120)
            if r.returncode != 0 or not os.path.exists(tmp):
                return None
            os.replace(tmp, tpath)   # atomic
            return tpath
        except (OSError, subprocess.SubprocessError):
            return None
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def ensure_thumb_async(media_dir, name):
    threading.Thread(target=ensure_thumb, args=(media_dir, name),
                     daemon=True).start()


def remove_thumb(media_dir, name):
    tpath = thumb_path(media_dir, name)
    if os.path.exists(tpath):
        try:
            os.remove(tpath)
        except OSError:
            pass
