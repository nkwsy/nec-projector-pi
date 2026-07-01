# Optional: stream the audio to phones

For a silent venue where people listen on their phones while the video plays on
the projector. The approach: **mpv's audio is tee'd to a loopback device, ffmpeg
encodes it to MP3, and Icecast fans it out** to any number of phones that open a
web page with an `<audio>` tag.

> **Set expectations:** latency is **~3–8 seconds** (browser buffering is the
> floor), so audio lags the picture. This is a "hear what's playing" feature,
> not lip-sync. Dozens of simultaneous listeners are fine. It is also the most
> hardware-dependent piece here — the ALSA tee needs tuning on the actual Pi.
> **Build/verify it last**, and know that a missing tee only disables this
> feature; normal playback is unaffected.

## Why this is fiddly

Running headless as `root`, there's no PipeWire/PulseAudio graph to capture from,
so the usual "grab the monitor source" recipes don't apply. Instead we duplicate
mpv's ALSA output to the real HDMI sink **and** a kernel loopback that ffmpeg
reads. ALSA's `multi`/`dmix` plumbing is famously picky about matching sample
rate/format against the vc4 HDMI device — budget an hour of trial-and-error.

## 1. Install

```bash
sudo apt install -y icecast2 ffmpeg
# During icecast2 setup, CHANGE the default 'hackme' passwords.
```

## 2. Kernel loopback

```bash
echo snd-aloop | sudo tee /etc/modules-load.d/aloop.conf
sudo modprobe snd-aloop
```

## 3. Tee mpv → HDMI + loopback (`/etc/asound.conf`)

```
pcm.projector_tee {
    type plug
    slave.pcm {
        type multi
        slaves.a.pcm "hw:vc4hdmi0,0"    # real HDMI out — CONFIRM name: aplay -l
        slaves.a.channels 2
        slaves.b.pcm "hw:Loopback,0,0"  # capture side reads hw:Loopback,1,0
        slaves.b.channels 2
        bindings.0 { slave a; channel 0 }
        bindings.1 { slave a; channel 1 }
        bindings.2 { slave b; channel 0 }
        bindings.3 { slave b; channel 1 }
    }
    ttable.0.0 1
    ttable.1.1 1
    ttable.0.2 1
    ttable.1.3 1
}
```

**Tuning:** vc4 HDMI usually only accepts `rate 48000`, `format S16_LE`,
`channels 2`; you may need to wrap the HDMI slave in `plug`/`dmix` to match.
Prototype before wiring ffmpeg:

```bash
speaker-test -D projector_tee -c 2 -t sine    # should play on the projector
arecord -D hw:Loopback,1,0 -f S16_LE -r 48000 -c 2 test.wav   # should capture
```

Then point mpv at the tee — add to `mpv_args` in `config.json`:

```json
"--audio-device=alsa/projector_tee"
```

## 4. Encode + serve

The loopback has no clock until mpv writes, so **start ffmpeg after playback
begins**:

```bash
ffmpeg -f alsa -channels 2 -sample_rate 48000 -i hw:Loopback,1,0 \
       -c:a libmp3lame -b:a 128k -f mp3 \
       icecast://source:YOUR_SOURCE_PW@127.0.0.1:8000/projector
```

Phones open `http://<pi-ip>:8000/projector` (directly — Icecast fans out; don't
proxy it through Flask). A simple page with `<audio controls src="…/projector">`
and a QR code to that URL works well.

## Wiring it into the app (sketch)

If you want a start/stop toggle in the panel, add an `audiocast.py` that
`subprocess.Popen`s the ffmpeg command above on play and kills it on stop
(mirroring `player.py`'s process handling), plus a phone-facing route that
serves the `<audio>` page. This was intentionally left uncoded — enable it here
once the ALSA tee is verified on your hardware.

## Alternative

For a single listener with less setup, use a **Bluetooth speaker/headset**
instead — see [bluetooth-audio.md](bluetooth-audio.md). Still not lip-sync, but
far fewer moving parts.
