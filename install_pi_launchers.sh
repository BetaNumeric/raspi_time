#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="${HOME}/Desktop"
AUTOSTART_DIR="${HOME}/.config/autostart"
ENABLE_AUTOSTART=0

if [[ "${1:-}" == "--autostart" ]]; then
    ENABLE_AUTOSTART=1
fi

mkdir -p "$DESKTOP_DIR"
mkdir -p "$AUTOSTART_DIR"

chmod +x "$SCRIPT_DIR/start_time_volume.sh" "$SCRIPT_DIR/stop_time_volume.sh" "$SCRIPT_DIR/install_boot_service.sh"

cat > "$DESKTOP_DIR/Start Time Volume.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Start Time Volume
Comment=Start the Time Volume server and fullscreen display
Path=$SCRIPT_DIR
Exec=$SCRIPT_DIR/start_time_volume.sh
Terminal=false
Categories=AudioVideo;Graphics;
EOF

cat > "$DESKTOP_DIR/Stop Time Volume.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Stop Time Volume
Comment=Stop the Time Volume server and fullscreen display
Path=$SCRIPT_DIR
Exec=$SCRIPT_DIR/stop_time_volume.sh
Terminal=false
Categories=AudioVideo;Graphics;
EOF

chmod +x "$DESKTOP_DIR/Start Time Volume.desktop" "$DESKTOP_DIR/Stop Time Volume.desktop"

if [[ "$ENABLE_AUTOSTART" == "1" ]]; then
    cp "$DESKTOP_DIR/Start Time Volume.desktop" "$AUTOSTART_DIR/Start Time Volume.desktop"
    chmod +x "$AUTOSTART_DIR/Start Time Volume.desktop"
fi

echo "Desktop launchers created on $DESKTOP_DIR"
if [[ "$ENABLE_AUTOSTART" == "1" ]]; then
    echo "Autostart enabled."
fi
