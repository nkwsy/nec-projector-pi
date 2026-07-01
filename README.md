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
sudo apt install -y python3-venv python3-pip mpv git
```

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

Edit `config.json` and set at least `projector_ip` (e.g. `192.168.0.10`).

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

Set the Pi up so nothing else owns the display:

```bash
# Boot to the text console instead of the desktop
sudo raspi-config nonint do_boot_behaviour B1
#   (or: sudo raspi-config → System Options → Boot / Auto Login → Console)

# Free tty1 so mpv can take over the screen (the service claims it)
sudo systemctl disable getty@tty1.service

# Confirm the modern KMS driver is active (default on Bookworm):
#   /boot/firmware/config.txt should contain  dtoverlay=vc4-kms-v3d
```

To modeset the screen `mpv` must become the DRM *master*, which on a bare
console needs root plus a free virtual terminal — that's why the systemd unit
(section 6) runs as `root` on `tty1`. After a reboot, test from the panel's
**Media** tab (upload a clip, tick it, press **Play Selected Now**). If audio
over HDMI is missing, add `--audio-device=...` to `mpv_args` (list devices with
`mpv --audio-device=help`; ALSA HDMI is typically `alsa/hdmi:CARD=vc4hdmi0`).

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

- **Library** — upload videos/images, delete them, tick the ones you want.
- **Play Selected Now** plays the ticked files immediately (looping if "Loop" is
  ticked). Multiple ticked files play as a playlist in list order.
- **New Schedule** — give it a name, tick files in the library, set a **Start**
  (and optional **End**) time and the **days** of the week. Options:
  - *Loop until end* — repeat the playlist until the end time.
  - *Power projector on at start* — powers up the projector, waits for it to
    warm up, opens the shutter, and selects the input automatically.
  - *Power off at end* — shuts the projector down when the schedule ends.
- Schedules persist to `schedules.json` and re-arm automatically when the
  service restarts.

Examples of what the scheduler covers:
- *"Play loop.mp4 at 18:00 on repeat until 22:00, every day"* → one file,
  Loop on, Start 18:00, End 22:00, all days.
- *"Play this series of videos starting at 09:00"* → tick the files in order,
  Start 09:00, leave End blank (or set one), Loop off for a single pass.

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
- **Security:** this panel has no login and is meant for a trusted home/LAN. Don't
  expose port 8080 to the internet. If you need remote access, use a VPN such as
  Tailscale or WireGuard rather than port-forwarding.

---

## 9. File overview

| File | Purpose |
|------|---------|
| `nec.py` | NEC binary control library (TCP 7142), all commands checksum-verified against NEC's reference manual |
| `player.py` | mpv playback controller (launch/stop/loop, IPC) |
| `app.py` | Flask web server, REST API, APScheduler timed jobs |
| `templates/index.html`, `static/` | the web UI |
| `config.example.json` | copy to `config.json` and edit |
| `projector-controller.service` | systemd autostart unit |

All projector command byte sequences come from NEC's *Projector Control Command
Reference Manual* (doc BDT140013/BDT140014) and were checksum-verified during build.
