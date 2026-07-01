# Optional: Bluetooth speaker output

The controller plays audio over **HDMI / USB / analog** out of the box (ALSA,
selectable live in the panel's **Audio output** dropdown). Bluetooth is *not*
enabled by default because it needs a sound server, and the cleanest way to get
one headless is to run the whole service as the `pi` user instead of `root` —
which changes how mpv gets the display. This guide walks through it.

> **Decide first:** Bluetooth A2DP adds ~120–250 ms of latency, so audio will
> lag the video — fine for ambient sound, wrong for lip-sync. If you only need
> one nearby listener with tight sync, a wired output is better.

## Why the architecture changes

BlueZ hands A2DP audio to a **per-user** PipeWire session, not to bare ALSA. A
`root` systemd service has no user PipeWire session (`XDG_RUNTIME_DIR` points at
`/run/user/0`, where nothing is running), so it can't see a Bluetooth sink. The
robust fix is to run the app as `pi` with a normal per-user PipeWire session and
keep it alive across logouts with *linger*.

## 1. Install the sound + Bluetooth stack

```bash
sudo apt install -y pipewire pipewire-pulse wireplumber libspa-0.2-bluetooth
# libspa-0.2-bluetooth is the crucial piece — without it PipeWire has no A2DP.
sudo loginctl enable-linger pi     # keep pi's PipeWire running with no login
```

## 2. Let the `pi` user drive the DRM/KMS display

Non-root DRM works if `pi` owns the VT and is in the right groups:

```bash
sudo usermod -aG video,render,audio,bluetooth pi
```

Edit `projector-controller.service` (follow the "Desktop alternative" comment
block in reverse — you want DRM, not desktop):

```ini
[Service]
User=pi
SupplementaryGroups=video render audio bluetooth
Environment=XDG_RUNTIME_DIR=/run/user/1000
TTYPath=/dev/tty1
StandardInput=tty-force
# keep RuntimeDirectory=projector, Restart=always, WorkingDirectory, ExecStart
```

Then in `config.json`, switch mpv to the PipeWire output so it can reach the BT
sink (ALSA still works for HDMI/USB):

```json
"mpv_args": ["--fullscreen", "--no-terminal", "--no-osc",
             "--vo=gpu", "--gpu-context=drm",
             "--ao=pipewire,alsa", "--keep-open=no"]
```

Reboot and **verify video still plays** (Play Selected Now). If DRM-as-`pi`
fights you, revert `User` to `root` + ALSA `mpv_args` — you'll keep HDMI/USB
audio but lose Bluetooth.

## 3. Pair the speaker

```bash
bluetoothctl
# in the prompt:
power on
agent on
default-agent
scan on                 # wait for your speaker's MAC to appear
pair  AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF  # 'trust' is what makes it auto-reconnect
connect AA:BB:CC:DD:EE:FF
quit
```

Make it reconnect on boot: enable `AutoEnable=true` under `[Policy]` in
`/etc/bluetooth/main.conf`, and (optional) add a small user oneshot that retries
`bluetoothctl connect AA:BB:CC:DD:EE:FF` a few times after boot.

## 4. Select it in the panel

Once connected it shows up in the **Audio output** dropdown (as a
`pipewire/bluez_output.*` device). Pick it; the change applies live. If lip-sync
matters, note the delay — a future `audio-delay` slider could compensate, but
A2DP lag varies by speaker.

## Troubleshooting

- **No BT device in the dropdown:** confirm `libspa-0.2-bluetooth` is installed
  and the service runs as `pi` (not root); check `sudo -u pi XDG_RUNTIME_DIR=/run/user/1000 wpctl status`.
- **Video broke after switching to `User=pi`:** DRM-as-non-root issue — verify
  `pi` is in `video`+`render` and the service has `TTYPath=/dev/tty1`; otherwise
  revert to root + ALSA.
- **Audio stutters/drops:** the built-in radio shares WiFi and BT — use wired
  Ethernet or a USB Bluetooth dongle (see [HARDWARE.md](../HARDWARE.md)).
