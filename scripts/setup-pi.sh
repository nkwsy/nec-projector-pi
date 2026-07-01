#!/usr/bin/env bash
#
# setup-pi.sh — one-shot headless appliance setup for the NEC projector controller.
#
# Configures Raspberry Pi OS Bookworm (Lite or Desktop, booted to console) for a
# clean "power on → black screen → video" experience and installs the extra
# system dependencies the app needs. Idempotent: safe to re-run. Every file it
# edits is backed up to <file>.bak-projector once.
#
# What it does:
#   1. apt install ffmpeg (thumbnails, previews, yt-dlp merging)
#   2. Boot cleanup: hide kernel text/cursor/rainbow, force one HDMI mode, so the
#      projector shows black instead of a blinking terminal before mpv starts.
#   3. Mask getty@tty1 so mpv can own the display on tty1.
#   4. Install a weekly yt-dlp auto-updater (yt-dlp breaks against YouTube often).
#   5. Optionally install + enable the systemd service.
#
# Usage:  sudo bash scripts/setup-pi.sh
#
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo: sudo bash scripts/setup-pi.sh" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$REPO_DIR/venv/bin/python"
BOOT=/boot/firmware
[[ -d $BOOT ]] || BOOT=/boot     # pre-Bookworm fallback
CFG="$BOOT/config.txt"
CMDLINE="$BOOT/cmdline.txt"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

backup_once() {
  local f=$1
  [[ -f $f && ! -f "$f.bak-projector" ]] && cp -a "$f" "$f.bak-projector" && echo "  backed up $f"
  return 0
}

ensure_line() {   # ensure_line <file> <exact line>
  local f=$1 line=$2
  grep -qxF "$line" "$f" 2>/dev/null || { echo "$line" >> "$f"; echo "  + $line -> $f"; }
}

# --- 1. dependencies -------------------------------------------------------
say "Installing ffmpeg (and mpv if missing)"
apt-get update -qq
apt-get install -y ffmpeg mpv >/dev/null
echo "  ffmpeg: $(command -v ffmpeg)"

# --- 2. boot cleanup -------------------------------------------------------
say "Configuring a clean, black boot"
if [[ -f $CFG ]]; then
  backup_once "$CFG"
  ensure_line "$CFG" "disable_splash=1"
  # KMS driver is required for DRM output; add it only if no vc4 overlay is set.
  grep -q "^dtoverlay=vc4-kms-v3d" "$CFG" || ensure_line "$CFG" "dtoverlay=vc4-kms-v3d"
else
  echo "  ! $CFG not found — skipping (edit your firmware config manually)"
fi

if [[ -f $CMDLINE ]]; then
  backup_once "$CMDLINE"
  # cmdline.txt MUST stay a single line. Move console off tty1 and quiet it down.
  line=$(tr -d '\n' < "$CMDLINE")
  line=${line//console=tty1/console=tty3}
  for opt in "loglevel=3" "logo.nologo" "vt.global_cursor_default=0" "consoleblank=0"; do
    key=${opt%%=*}
    grep -qw -- "$key" <<<"$line" || line="$line $opt"
  done
  printf '%s\n' "$line" > "$CMDLINE"
  echo "  cmdline.txt updated (console -> tty3, cursor/logo hidden)"
else
  echo "  ! $CMDLINE not found — skipping"
fi

# --- 3. free tty1 for mpv --------------------------------------------------
say "Masking getty@tty1 so mpv can own the screen"
systemctl mask getty@tty1.service >/dev/null 2>&1 || true
echo "  getty@tty1 masked"

# --- 4. yt-dlp weekly auto-update -----------------------------------------
if [[ -x $VENV_PY ]]; then
  say "Installing weekly yt-dlp auto-updater"
  cat >/etc/systemd/system/ytdlp-update.service <<EOF
[Unit]
Description=Update yt-dlp in the projector controller venv

[Service]
Type=oneshot
ExecStart=$VENV_PY -m pip install -U yt-dlp
ExecStartPost=/bin/systemctl restart projector-controller.service
EOF
  cat >/etc/systemd/system/ytdlp-update.timer <<'EOF'
[Unit]
Description=Weekly yt-dlp update

[Timer]
OnCalendar=Sun 04:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now ytdlp-update.timer >/dev/null
  echo "  ytdlp-update.timer enabled (Sundays 04:00)"
else
  echo "  ! venv not found at $VENV_PY — create it first (README §4); skipping updater"
fi

# --- 5. install the service (optional) ------------------------------------
say "Service install"
read -r -p "Install & enable projector-controller.service now? [y/N] " yn
if [[ ${yn,,} == y ]]; then
  sed "s#/home/pi/nec-projector-pi#$REPO_DIR#g" "$REPO_DIR/projector-controller.service" \
    > /etc/systemd/system/projector-controller.service
  systemctl daemon-reload
  systemctl enable projector-controller.service >/dev/null
  echo "  installed. Start now with: sudo systemctl start projector-controller"
else
  echo "  skipped."
fi

say "Done. Reboot to get the clean black boot: sudo reboot"
echo "If anything looks wrong, restore the *.bak-projector files in $BOOT and reboot."
