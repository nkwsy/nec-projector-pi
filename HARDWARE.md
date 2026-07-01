# Hardware guide

What to buy for a projector-controller Pi that plays 1080p (and occasional 4K)
video, image slideshows, RTSP camera feeds, downloads videos, and drives
flexible audio out.

## TL;DR

**Raspberry Pi 5 (8 GB) + official Active Cooler + official 27 W USB-C PSU + a
USB 3.0 SSD (or NVMe via the M.2 HAT+) for the media library.** Use HDMI audio by
default. Prefer wired Ethernet if you use RTSP.

## Why Pi 5 (and the one Pi 4 caveat)

- **Pi 5** *removed* the hardware **H.264** decoder but keeps a **4K60 HEVC/H.265**
  hardware decoder. Its four Cortex-A76 cores software-decode 1080p H.264/VP9/AV1
  easily — in fact faster than the Pi 4 does H.264 in *hardware*. 4K H.265 is
  hardware-accelerated; 4K H.264/VP9 are software (fine at moderate bitrate);
  4K AV1 is marginal — don't rely on it.
- **Pi 4** keeps hardware H.264 **and** a 3.5 mm analog jack. Only worth it as a
  hand-me-down, not a new purchase in 2026.
- The app already uses `--hwdec=auto-safe`, which picks the right decode path on
  both boards and never hard-fails on H.264 (don't force `--hwdec=v4l2m2m` on Pi 5).

## Bill of materials

| Item | Role | Recommended (~$210) | Budget (~$150–170) |
|---|---|---|---|
| **Board** | CPU / decode | Raspberry Pi 5, **8 GB** (~$80) | Pi 5, **4 GB** (~$60) |
| **Cooling** | Prevent throttling on sustained decode/ffmpeg | Official **Active Cooler** (~$10) — mandatory | Active Cooler or fan case (~$10) |
| **PSU** | Stable power for board + SSD | Official **27 W USB-C PD** (~$14) — mandatory; cheap chargers corrupt SSDs under load | Official 27 W PD (~$14) |
| **Storage (OS + media)** | Durability, no SD wear | **NVMe 512 GB–1 TB (2242)** + official **M.2 HAT+** (~$20 HAT + $50–90 SSD). HAT+ fits 2230/2242 only, *not* 2280 | **USB 3.0 SSD 500 GB**, boot off USB-A (~$45–60) |
| SD (optional) | Recovery/fallback | — | microSD A2 high-endurance 32 GB (~$10) |
| **HDMI cable** | Video + default HDMI audio | ~$8 | ~$8 |
| **Case** | Enclosure w/ cooler + HAT clearance | ~$15–25 | Official Pi 5 case w/ fan (~$10) |
| **Ethernet cable** | RTSP/stream jitter; frees the WiFi/BT radio | ~$5 (prefer wired) | ~$5 |
| USB DAC *(optional)* | Analog line-out (Pi 5 has **no** 3.5 mm jack) | $15–40 — only if feeding a mixer/amp | add later |
| HDMI audio de-embedder *(optional)* | External sound if the projector has no audio-out | $20–35 | — |
| USB BT 5.x dongle *(optional)* | Offload Bluetooth from the shared WiFi radio | $10–15 — only if using BT audio | — |

## Audio at a glance

- **Default = HDMI** to the projector: perfect digital, zero latency, in sync, no
  extra hardware. Pick it in the panel's **Audio output** dropdown (it's `auto`
  by default, which resolves to HDMI on a headless Pi).
- **USB DAC** for analog line-level into a mixer/amp (the only analog option on
  Pi 5). Plug it in and select it in the dropdown.
- **Bluetooth speaker** and **streaming audio to phones** are supported but need
  extra OS setup — see [docs/bluetooth-audio.md](docs/bluetooth-audio.md) and
  [docs/audio-to-phone.md](docs/audio-to-phone.md).

## Sizing & networking notes

- Media: 1080p ≈ 1–3 GB/hr, 4K ≈ 5–15 GB/hr. yt-dlp muxing can transiently
  double a file's size — keep 20 %+ free. 500 GB–1 TB recommended if you download.
- Don't run a growing media library off a microSD (wears out under download/temp
  writes). Use a USB/NVMe SSD.
- Prefer wired Ethernet for RTSP and for audio streaming; the Pi's built-in
  Bluetooth shares the WiFi radio, so a USB BT dongle helps if you use both.
