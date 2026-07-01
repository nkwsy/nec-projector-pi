# NEC PX803UL Web Controller

A self-hosted web panel that runs on a Raspberry Pi and controls an NEC PX803UL
projector over the network (power, shutter, lens focus / zoom / shift, input,
mute, freeze), plays media out of the Pi's HDMI into the projector, and runs a
scheduler ("play this video at 18:00 on repeat until 22:00", "play this playlist
starting at a set time").

---

## 1. How it fits together

```
   ┌───────────────┐   Ethernet (control, TCP 7142)   ┌──────────────┐
   │ Raspberry Pi  │─────────────────────────────────▶│   PX803UL    │
   │               │   HDMI (video out → projector IN) │  projector   │
   │  eth0 ────────┼─────────────────────────────────▶│              │
   │  wlan0 ─ Wi-Fi│                                   └──────────────┘
   └──────┬────────┘
          │  you open the web panel from your phone/laptop on the same Wi-Fi
          ▼
     http://<pi-wifi-ip>:8080
```

Two cables run from the Pi to the projector:

- **Ethernet** carries the control commands (NEC's binary protocol on TCP port 7142).
- **HDMI** carries the actual video. The Pi plays files with `mpv` and outputs
  fullscreen; the projector just displays its HDMI input.

The Pi stays on your Wi-Fi (`wlan0`) so you can reach the panel from any device.
The Ethernet port (`eth0`) is a dedicated link to the projector.

> If you'd rather put the projector on your main LAN and skip the dedicated
> Ethernet link, that works too — just set `projector_ip` in `config.json` to
> wherever the projector lives and ignore the static-IP step below.

---

## 2. Prepare the Raspberry Pi

Use a Pi 4 or 5 (the Pi 4/5 handle 1080p/4K HDMI playback comfortably). Flash
**Raspberry Pi OS (Bookworm)** with the Raspberry Pi Imager, and in the imager's
settings preconfigure your Wi-Fi, hostname, and SSH so it joins your network on
first boot.

After it boots, SSH in and update:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-venv python3-pip mpv ffmpeg git
```

`ffmpeg` powers thumbnails, in-browser previews, and merging downloaded videos.
For hardware recommendations (Pi model, storage, audio, cooling) see
[HARDWARE.md](HARDWARE.md).

---

## 3. Network setup

### 3a. Projector side

By default NEC PX803UL ships with DHCP **off** and a static address of
`192.168.0.10`. On the projector's on-screen menu, confirm/set:

- Menu → **Setup → Installation(2) → Network Settings → Wired LAN**
- `DHCP: OFF`, `IP ADDRESS: 192.168.0.10`, `SUBNET MASK: 255.255.255.0`
- Make sure network/PJLink control is enabled and the control password (if any)
  is noted. Also confirm **Standby Mode** allows network control if you want to
  power the projector on remotely (Menu → Setup → Options(2) → Standby Mode →
  *Network Standby*), otherwise it can't be woken over LAN.

### 3b. Pi side — give eth0 a static address on the projector's subnet

Bookworm uses NetworkManager. Give the wired port a fixed IP in the projector's
range (no gateway, so your default route stays on Wi-Fi):

```bash
sudo nmcli con add type ethernet ifname eth0 con-name proj \
  ipv4.method manual ipv4.addresses 192.168.0.2/24
sudo nmcli con up proj
```

Verify you can reach the projector:

```bash
ping -c2 192.168.0.10
# and confirm the control port is open:
nc -vz 192.168.0.10 7142
```

Your Wi-Fi (`wlan0`) keeps its own DHCP address from your router — that's the
address you'll use to open the panel. Find it with `hostname -I`.

---

## 4. Install the controller

Copy this folder to the Pi (e.g. `scp -r projector-controller pi@<pi-ip>:~/`
or `git clone`), then:

```bash
cd ~/projector-controller
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

Edit `config.json` and set at least `projector_ip` (e.g. `192.168.0.10`) and an
`auth_password` (the panel is otherwise unprotected — see [§8](#8-security)).

Run it:

```bash
./venv/bin/python app.py
```

Open `http://<pi-wifi-ip>:8080` from your phone or laptop. The header should show
the projector model and power state. If it says *unreachable*, recheck section 3.

---

## 5. HDMI video output (mpv)

How `mpv` reaches the screen depends on whether your Pi runs a desktop.

**Headless (recommended for an appliance — no desktop):** `mpv` renders straight
to the projector via DRM/KMS. This is the default `config.json` shipped here:

```json
"mpv_args": ["--fullscreen", "--no-terminal", "--no-osc",
             "--vo=gpu", "--gpu-context=drm", "--keep-open=no"]
```

**Elegant boot — one command.** The included setup script boots the Pi to a
clean **black** screen (no rainbow, no kernel text, no blinking cursor) and
installs the extras this app needs. Run it once:

```bash
sudo bash scripts/setup-pi.sh
```

It: installs `ffmpeg`; edits `/boot/firmware/{config.txt,cmdline.txt}` to hide
the boot console (redirects it to a hidden VT, kills the cursor/logo/rainbow);
masks `getty@tty1` so `mpv` can own the screen; installs a weekly `yt-dlp`
auto-updater; and optionally installs the service. Every file it changes is
backed up to `*.bak-projector`. Reboot afterwards.

**How the black screen stays black:** the app starts **one long-lived `mpv`** at
boot with `--idle --force-window`, so it grabs the DRM/KMS display the instant
the Pi is up and holds a solid black frame. Content is swapped in over mpv's IPC
socket — no per-clip relaunch, so there's no "no-signal" flash between videos,
and audio device/mute changes apply live.

To modeset the screen `mpv` must become the DRM *master*, which on a bare
console needs root plus a free virtual terminal — that's why the systemd unit
(section 6) runs as `root` on `tty1`. After a reboot, test from the panel's
**Media** tab (upload a clip, tick it, press **Play Selected Now**). HDMI audio
works by default; pick a different output in the panel's **Audio output**
dropdown (see [§7](#7-using-the-panel)).

> Prefer to do it by hand? The equivalent manual steps are: `sudo raspi-config
> nonint do_boot_behaviour B1` (boot to console), `sudo systemctl disable
> getty@tty1`, and confirm `dtoverlay=vc4-kms-v3d` in `/boot/firmware/config.txt`.
> You'll get working playback but not the fully clean boot the script sets up.

**Raspberry Pi OS with Desktop (alternative):** switch `mpv_args` back to the
non-DRM defaults (drop `--vo=gpu`/`--gpu-context=drm`) and edit the service unit
as noted in its comments. Bookworm's desktop is Wayland, so `mpv` needs
`WAYLAND_DISPLAY`; `player.py` auto-detects the compositor socket from
`XDG_RUNTIME_DIR`, and the unit's `Environment=` lines cover the service case.
Enable Desktop Autologin so a session exists for `mpv` to draw into.

---

## 6. Run it automatically on boot

A `systemd` unit is included. Install it:

```bash
sudo cp projector-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now projector-controller
sudo systemctl status projector-controller      # check it's running
journalctl -u projector-controller -f           # live logs
```

The unit assumes the repo lives at `/home/pi/nec-projector-pi`; edit
`WorkingDirectory`/`ExecStart` if yours differ. As shipped it's configured for
the **headless** setup from section 5 (runs as `root` on `tty1`); the file's
comments show what to change for a desktop session instead.

---

## 7. Using the panel

**Control tab**

- **Power / Shutter / Mute / Freeze** — one-tap buttons.
- **Input** — switch the projector's source. The presets cover the usual
  terminals; if one errors for your firmware, use the Raw command box (below)
  to find the right code, then it's reliable.
- **Lens** — the D-pad does lens *shift* (up/down/left/right), and the rows below
  do *focus* and *zoom*. Each tap nudges the motor for the **step size** selected
  (0.25 / 0.5 / 1.0 s). The red ■ stops all lens motors. *Read Positions* shows
  the current encoder values and limits for each axis.
- **Lens Memory** — recall or store a saved lens position (handy if you switch
  between screen formats). Run a **Lens Calibration** from the projector's own
  remote once after installing/replacing a lens so the ranges are accurate.
- **Raw command** — send arbitrary hex frames for testing. Tick "add checksum"
  to let the app append the trailing checksum byte for you. Example to try HDMI
  input code `0x1A`: `02 03 00 00 02 01 1A` (with checksum ticked).

**Media & Schedule tab**

- **Add Media** — upload a file, or paste a **YouTube/Vimeo URL** to download it
  straight into the library (up to 1080p, progress shown live; Cancel to abort).
  Only allow-listed sites are accepted. To support more sites, add hostnames to
  `download_allowed_hosts` in `config.json`; for age/bot-gated videos set
  `download_cookiefile` to a Netscape `cookies.txt` path.
- **Library** — a thumbnail grid. Click any thumbnail to **preview** it in the
  browser (video for mp4/webm/images; other containers show a poster frame and
  still play fine on the projector). Tick items to select them.
- **Play Selected Now** plays the ticked files immediately as a playlist in list
  order (looping if "Loop" is ticked). Tick **As slideshow** + a seconds/image
  value to show images (or mixed images+clips) as a timed slideshow.
- **Live RTSP stream** — paste an `rtsp://` camera/stream URL and **Play Stream**.
  It auto-reconnects if the stream drops. Press **Stop** to return to black.
- **Audio output** — pick HDMI / USB / analog from the dropdown; changes apply
  live. **Mute** toggles sound without stopping playback. (Bluetooth and
  streaming audio to phones need extra setup — see [§9](#9-optional-audio-outputs).)
- **New Schedule** — name it, pick a **Source type** (video files, image
  slideshow, or RTSP), then:
  - *Files/slideshow*: tick files and **drag (or use ↑↓) to set play order**.
    Slideshow adds a per-image duration and optional shuffle.
  - *RTSP*: enter the stream URL.
  - Set **Start** (and optional **End**) time and the **days** of the week.
  - *Loop until end* — repeat until the end time.
  - *Power projector on at start* — powers up, warms up, opens the shutter, and
    selects the input automatically. *Power off at end* — shuts it down at End.
- Schedules persist to `schedules.json` and re-arm automatically when the
  service restarts.

Examples of what the scheduler covers:
- *"Play loop.mp4 at 18:00 on repeat until 22:00, every day"* → Files source,
  one file, Loop on, Start 18:00, End 22:00, all days.
- *"Play this series of videos starting at 09:00"* → Files source, tick files and
  order them, Start 09:00, leave End blank, Loop off for a single pass.
- *"Show the lobby photos on a 10-second loop"* → Slideshow source, tick images,
  10 s/image, Loop on.
- *"Put the entrance camera up during the event"* → RTSP source with the camera
  URL.

---

## 8. Troubleshooting & notes

- **Header shows "unreachable":** ping the projector from the Pi (section 3b);
  confirm port 7142 is open; confirm network control is enabled on the projector.
- **Nothing shows on the projector when you press Play (desktop):** Bookworm's
  desktop is Wayland, so `mpv` needs `WAYLAND_DISPLAY`. Test from a desktop
  terminal with `WAYLAND_DISPLAY=wayland-0 mpv --fullscreen yourclip.mp4`. If
  that works but the service doesn't, enable **desktop autologin** (section 5) —
  the service needs the logged-in session's `/run/user/1000` to exist. Confirm
  the socket name with `ls /run/user/1000/wayland-*` (it's usually `wayland-0`);
  set it in the service's `WAYLAND_DISPLAY=` line if different.
- **Power On over LAN does nothing:** the projector must be in *Network Standby*
  (not the deepest power-saving standby) to accept a wake command. See 3a.
- **Right after Power On, other commands fail:** the projector ignores commands
  while warming up. The scheduler already waits ~35 s before opening the shutter;
  if you script your own sequence, allow similar time.
- **Input preset errors:** HDMI/DisplayPort/HDBaseT codes can differ slightly by
  firmware. Use the Raw command tester to find the working code, then tell me and
  I'll bake it into `INPUTS` in `nec.py` (or edit that dict yourself).
- **Lens won't reach expected focus/zoom:** run **Lens Calibration** from the
  projector remote (INFO/L-CALIB. while holding CTL) after any lens change.
- **"yt-dlp not installed" / "ffmpeg not found" banner:** install them
  (`sudo apt install ffmpeg`; `./venv/bin/pip install -U yt-dlp`) and restart.
- **Downloads suddenly fail from YouTube:** yt-dlp goes stale every few weeks.
  `scripts/setup-pi.sh` installs a weekly updater; force it with
  `sudo systemctl start ytdlp-update`.
- **No thumbnails / previews:** `ffmpeg` is missing, or the file is a container
  the browser can't play inline (mkv/avi/mov show a poster frame instead).
- **RTSP won't play:** confirm the URL works in VLC/`ffprobe` from the Pi; the
  app forces TCP transport for reliability. Credentials in the URL are redacted
  from status/logs.

### Security

You set an `auth_password` in `config.json`, so the panel prompts for a login
(user `auth_user`, default `admin`). Still treat it as a **trusted-LAN**
appliance:

- **Never expose port 8080 to the internet.** It runs as root and can send raw
  bytes to the projector and download arbitrary URLs. For remote access use a VPN
  (Tailscale/WireGuard), not port-forwarding.
- Basic-auth is only as private as the network — put it behind HTTPS (a reverse
  proxy) if you want the password encrypted in transit.
- The download feature is limited to an allow-list of sites and refuses URLs that
  resolve to private/loopback addresses (anti-SSRF). RTSP URLs are scheme-checked.

---

## 9. Optional audio outputs

HDMI / USB / analog output works out of the box. Two extras need OS-level setup
and are documented separately:

- **Bluetooth speaker** — [docs/bluetooth-audio.md](docs/bluetooth-audio.md).
  Requires running the service as the `pi` user with PipeWire; adds ~150–250 ms
  latency (not lip-sync).
- **Stream audio to phones** (browser, for silent venues) —
  [docs/audio-to-phone.md](docs/audio-to-phone.md). Icecast + an ffmpeg/ALSA
  tee; ~3–8 s latency; fans out to many listeners.

---

## 10. File overview

| File | Purpose |
|------|---------|
| `nec.py` | NEC binary control library (TCP 7142), all commands checksum-verified against NEC's reference manual |
| `player.py` | mpv playback controller — one persistent idle-black mpv; files, slideshow, RTSP, live audio, all over IPC |
| `downloader.py` | yt-dlp download jobs (background threads, URL allow-listing, progress) |
| `media_util.py` | safe media-path resolution, ffmpeg thumbnails, format helpers |
| `app.py` | Flask web server, REST API, auth gate, APScheduler timed jobs |
| `index.html`, `app.js`, `style.css` | the web UI (also works under `templates/` + `static/`) |
| `config.example.json` | copy to `config.json` and edit |
| `projector-controller.service` | systemd autostart unit (headless, root on tty1) |
| `scripts/setup-pi.sh` | one-shot headless setup: ffmpeg, clean boot, getty mask, yt-dlp updater |
| `HARDWARE.md`, `docs/` | hardware BOM and optional-audio (Bluetooth, phone-streaming) guides |

All projector command byte sequences come from NEC's *Projector Control Command
Reference Manual* (doc BDT140013/BDT140014) and were checksum-verified during build.
