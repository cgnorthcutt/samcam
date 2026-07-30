#!/usr/bin/env bash
# Watch how the camera enumerates on the USB bus, live.
#
# Run this, then try every way of attaching the camera -- plug in while off,
# power on first then plug in, hold the mode button while connecting, tap mode
# once connected. Each time the bus changes you get a line here.
#
# What you are looking for is the interface class:
#   class 8  = mass storage -> files only, no live video, ever
#   class 14 = USB video (UVC) -> a real camera, live streaming works
#
# If nothing you try ever prints class 14, the unit has no PC-camera mode and
# live streaming over USB is impossible with this hardware.

set -uo pipefail

class_name() {
  case "$1" in
    1)  echo "audio" ;;
    3)  echo "HID" ;;
    8)  echo "mass storage" ;;
    14) echo "VIDEO (UVC)" ;;
    239) echo "misc/composite" ;;
    255) echo "vendor-specific" ;;
    *)  echo "class $1" ;;
  esac
}

snapshot() {
  ioreg -w0 -r -c IOUSBHostInterface -l 2>/dev/null \
    | grep -o '"USB Device Info" = {[^}]*}' \
    | sed -E 's/.*"USB Product Name"="([^"]*)".*"idVendor"=([0-9]+).*"bInterfaceClass"=([0-9]+).*/\2|\1|\3/' \
    | sort -u
}

echo "watching the USB bus -- ctrl-c to stop"
echo "(plug the camera in different ways and watch what changes)"
echo

previous=""
while true; do
  current=$(snapshot)
  if [[ "$current" != "$previous" ]]; then
    echo "── $(date '+%H:%M:%S') ──────────────────────────────"
    if [[ -z "$current" ]]; then
      echo "   (no USB devices)"
    else
      while IFS='|' read -r vid name cls; do
        [[ -z "$cls" ]] && continue
        printf '   %-28s vendor %-6s %s\n' "${name:-unknown}" "$(printf '0x%04x' "$vid")" "$(class_name "$cls")"
        if [[ "$cls" == "14" ]]; then
          echo "   ^^ UVC video interface -- this one can stream live!"
        fi
      done <<< "$current"
    fi

    cams=$(system_profiler SPCameraDataType 2>/dev/null | grep -E '^\s{4}\S.*:$' | sed 's/[[:space:]]*//;s/:$//')
    [[ -n "$cams" ]] && echo "   macOS cameras: $(echo "$cams" | paste -sd ', ' -)"
    echo
    previous="$current"
  fi
  sleep 1
done
