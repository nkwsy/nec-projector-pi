"""
downloader.py - Download remote videos into media_dir via yt-dlp.

One background thread per job; status is polled by the web UI (mirrors the
polling model used elsewhere in the app). URLs are validated (scheme + host
allowlist + private-IP block) BEFORE yt-dlp sees them, and the generic
extractor is disabled, to blunt SSRF / arbitrary-URL abuse. yt-dlp is imported
lazily so the app still runs (with downloads disabled) if it isn't installed.
"""

import glob
import ipaddress
import os
import socket
import threading
import uuid
from urllib.parse import urlparse

try:
    import yt_dlp
    from yt_dlp.utils import (DownloadError, DownloadCancelled,
                              ExtractorError, GeoRestrictedError)
    YTDLP_OK = True
except Exception:                        # pragma: no cover - optional dep
    yt_dlp = None
    YTDLP_OK = False

    class DownloadCancelled(Exception):
        pass

    class DownloadError(Exception):
        pass

    class ExtractorError(Exception):
        pass

    class GeoRestrictedError(Exception):
        pass


# Sites we intend to support. Hostname allowlisting is the primary SSRF defense;
# the private-IP resolve check below is a second layer against DNS tricks.
DEFAULT_ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "vimeo.com", "player.vimeo.com",
}


def ytdlp_version():
    if not YTDLP_OK:
        return None
    return getattr(getattr(yt_dlp, "version", None), "__version__", None)


class Downloader:
    def __init__(self, media_dir, max_height=1080, merge_format="mkv",
                 allowed_hosts=None, cookiefile=None):
        self.media_dir = media_dir
        self.max_height = int(max_height)
        self.merge_format = merge_format
        self.allowed_hosts = set(allowed_hosts) if allowed_hosts else set(DEFAULT_ALLOWED_HOSTS)
        self.cookiefile = cookiefile if (cookiefile and os.path.exists(cookiefile)) else None
        self.jobs = {}                   # id -> status dict
        self.lock = threading.Lock()
        self._seq = 0                    # monotonic job counter (id is random)
        # Downloads run one at a time: keeps a Pi from thrashing on parallel
        # muxes AND makes _cleanup_partials safe (only this job's partials exist).
        self._runlock = threading.Lock()

    # ---- validation -------------------------------------------------------
    def validate_url(self, url):
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            raise ValueError("Only http/https URLs are allowed")
        host = (u.hostname or "").lower()
        if host not in self.allowed_hosts:
            raise ValueError(f"Host not allowed: {host or '(none)'}")
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            raise ValueError("Host does not resolve")
        for *_, sockaddr in infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                raise ValueError("Host resolves to a non-public address")
        return url

    # ---- public API -------------------------------------------------------
    def start(self, url):
        if not YTDLP_OK:
            raise RuntimeError("yt-dlp is not installed on the Pi "
                               "(pip install yt-dlp)")
        url = self.validate_url(url.strip())
        jid = uuid.uuid4().hex[:8]
        with self.lock:
            self._prune_locked()
            self._seq += 1
            self.jobs[jid] = {"id": jid, "seq": self._seq, "url": url,
                              "state": "queued", "percent": None, "speed": None,
                              "eta": None, "title": None, "filename": None,
                              "error": None, "cancel": False}
        threading.Thread(target=self._run, args=(jid, url), daemon=True).start()
        return jid

    def status(self, jid=None):
        with self.lock:
            if jid is None:
                # oldest-first by creation order; UI reverses for newest-first
                return [self._public(j) for j in
                        sorted(self.jobs.values(), key=lambda j: j["seq"])]
            return self._public(self.jobs[jid]) if jid in self.jobs else None

    def cancel(self, jid):
        with self.lock:
            if jid in self.jobs:
                self.jobs[jid]["cancel"] = True
                return True
        return False

    # ---- internals --------------------------------------------------------
    @staticmethod
    def _public(job):
        return {k: v for k, v in job.items() if k != "cancel"}

    def _prune_locked(self):
        # Keep the job dict bounded: drop oldest finished jobs beyond ~20.
        done = [j for j in self.jobs.values()
                if j["state"] in ("done", "error", "cancelled")]
        if len(self.jobs) > 20 and done:
            for j in sorted(done, key=lambda j: j["seq"])[:len(self.jobs) - 20]:
                self.jobs.pop(j["id"], None)

    def _set(self, jid, **kw):
        with self.lock:
            if jid in self.jobs:
                self.jobs[jid].update(kw)

    def _hook(self, jid, d):
        # Keep this cheap — heavy work here can stall/break yt-dlp.
        with self.lock:
            if self.jobs.get(jid, {}).get("cancel"):
                raise DownloadCancelled("cancelled by user")
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            got = d.get("downloaded_bytes")
            self._set(jid, state="downloading",
                      percent=round(got / total * 100, 1) if total and got else None,
                      speed=d.get("speed"), eta=d.get("eta"))
        elif status == "finished":
            # A stream finished; a merge/mux may still follow.
            self._set(jid, state="processing", percent=100)

    def _opts(self, jid):
        h = self.max_height
        opts = {
            "paths": {"home": self.media_dir},
            "outtmpl": "%(title).120s [%(id)s].%(ext)s",   # must end in .ext
            "format": f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b",
            "merge_output_format": self.merge_format,
            "restrictfilenames": True,
            "trim_file_name": 200,
            "noplaylist": True,
            "ignoreconfig": True,                          # ignore planted configs
            "allowed_extractors": ["default", "-generic"],  # SSRF/RCE hardening
            "socket_timeout": 30, "retries": 10, "fragment_retries": 10,
            "writesubtitles": False, "writeautomaticsub": False,
            "progress_hooks": [lambda d: self._hook(jid, d)],
            "quiet": True, "no_warnings": True, "noprogress": True,
            # NB: never set "netrc_cmd" (CVE-2026-26331 command injection).
        }
        if self.cookiefile:
            opts["cookiefile"] = self.cookiefile
        return opts

    def _run(self, jid, url):
        # Serialize: one download at a time, so _cleanup_partials on cancel only
        # ever sees this job's leftovers. Other queued jobs wait here.
        with self._runlock:
            with self.lock:
                if self.jobs.get(jid, {}).get("cancel"):
                    self.jobs[jid]["state"] = "cancelled"
                    return
            self._download(jid, url)

    def _download(self, jid, url):
        self._set(jid, state="starting")
        try:
            with yt_dlp.YoutubeDL(self._opts(jid)) as ydl:
                info = ydl.extract_info(url, download=True)
            self._set(jid, title=info.get("title"))
            reqs = info.get("requested_downloads") or []
            path = reqs[0].get("filepath") if reqs else None
            if path:
                real = os.path.realpath(path)
                base = os.path.realpath(self.media_dir)
                if os.path.commonpath([real, base]) != base:
                    raise RuntimeError("downloaded file escaped media_dir")
                self._set(jid, state="done", percent=100,
                          filename=os.path.basename(real))
            else:
                self._set(jid, state="done", percent=100)
        except DownloadCancelled:
            self._set(jid, state="cancelled")
            self._cleanup_partials()
        except GeoRestrictedError:
            self._set(jid, state="error", error="Not available in your region")
        except (DownloadError, ExtractorError) as e:
            msg = str(e)
            low = msg.lower()
            if "confirm you" in low or "age" in low or "sign in" in low:
                msg = "Blocked (age/bot check) — cookies required"
            self._set(jid, state="error", error=msg[:300])
        except Exception as e:
            self._set(jid, state="error", error=str(e)[:300])

    def _cleanup_partials(self):
        for pat in ("*.part", "*.ytdl", "*.part-Frag*"):
            for f in glob.glob(os.path.join(self.media_dir, pat)):
                try:
                    os.remove(f)
                except OSError:
                    pass
