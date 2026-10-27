#!/usr/bin/env bash
# iPodify setup for Arch / Omarchy. Safe to re-run. Asks for sudo where needed.
#  - system packages: libgpod, ffmpeg, chromium, pipewire-pulse (pacat/parec), librespot (optional player)
#  - hfsprogs from the AUR (fsck.hfsplus, to repair the iPod's filesystem if needed)
#  - Python venv with the pinned dependencies
#  - builds bin/ipod-helper (libgpod)
#  - installs an app-launcher entry
#  - checks/repairs a connected iPod whose HFS+ is flagged as not cleanly unmounted
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

sudo pacman -S --needed --noconfirm libgpod ffmpeg chromium pipewire-pulse librespot python gcc make pkgconf
if ! command -v fsck.hfsplus >/dev/null; then
  if command -v yay >/dev/null; then
    yay -S --needed --noconfirm hfsprogs
  else
    echo "! hfsprogs (AUR) not installed – needed only to repair the iPod's filesystem"
  fi
fi

if [[ ! -x $here/.venv/bin/python ]]; then
  python3 -m venv "$here/.venv"
fi
"$here/.venv/bin/pip" install --quiet --upgrade pip
"$here/.venv/bin/pip" install --quiet -r "$here/requirements.txt"

make -C "$here/helper" --no-print-directory

apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$apps"
cat >"$apps/ipodify.desktop" <<EOF
[Desktop Entry]
Version=1.0
Name=iPodify
Comment=Spotify & podcasts → iPod, retro style
Exec=xdg-terminal-exec --app-id=TUI.ipodify -e $here/ipodify.sh
Terminal=false
Type=Application
Icon=multimedia-player
Categories=Audio;Music;
StartupNotify=true
EOF

part="$(lsblk -rpno NAME,FSTYPE | awk '$2=="hfsplus" {print $1}' | while read -r p; do
  m="$(findmnt -rn -o TARGET "$p" | head -1)"; m="$(printf '%b' "$m")"
  [[ -z $m || -d $m/iPod_Control ]] && echo "$p" && break; done || true)"
if [[ -n $part ]] && command -v fsck.hfsplus >/dev/null &&
   journalctl -k -b --no-pager 2>/dev/null | grep -q "hfsplus: Filesystem was not cleanly unmounted"; then
  echo "→ checking the iPod's filesystem on $part (fsck.hfsplus)"
  udisksctl unmount -b "$part" 2>/dev/null || true
  sudo fsck.hfsplus -f -y "$part" || echo "! fsck reported problems – see output above"
  udisksctl mount -b "$part" >/dev/null || true
fi

echo "✓ Done. Start iPodify from the app launcher or with $here/ipodify.sh"
