#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="${HOME}/Desktop"
AUTOSTART_DIR="${HOME}/.config/autostart"
ENABLE_AUTOSTART=0
START_ENV_ARGS="TIME_VOLUME_DISPLAY_BACKEND=mpv"

if [[ -n "${TIME_VOLUME_AUTO_START_CYCLE:-}" ]]; then
    START_ENV_ARGS="$START_ENV_ARGS TIME_VOLUME_AUTO_START_CYCLE=$TIME_VOLUME_AUTO_START_CYCLE"
fi

if [[ -n "${TIME_VOLUME_AUTO_START_CYCLE_DELAY_SEC:-}" ]]; then
    START_ENV_ARGS="$START_ENV_ARGS TIME_VOLUME_AUTO_START_CYCLE_DELAY_SEC=$TIME_VOLUME_AUTO_START_CYCLE_DELAY_SEC"
fi

if [[ "${1:-}" == "--autostart" ]]; then
    ENABLE_AUTOSTART=1
fi

mkdir -p "$DESKTOP_DIR"
mkdir -p "$AUTOSTART_DIR"

chmod +x \
    "$SCRIPT_DIR/start_time_volume.sh" \
    "$SCRIPT_DIR/start_time_volume_autostart.sh" \
    "$SCRIPT_DIR/start_time_volume_display.sh" \
    "$SCRIPT_DIR/stop_time_volume.sh" \
    "$SCRIPT_DIR/install_boot_service.sh"

cat > "$DESKTOP_DIR/Start Time Volume.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Start Time Volume
Comment=Start the Time Volume server and fullscreen display
Path=$SCRIPT_DIR
Exec=env $START_ENV_ARGS $SCRIPT_DIR/start_time_volume.sh
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

cat > "$DESKTOP_DIR/Start Time Volume Display.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Start Time Volume Display
Comment=Open or restart the fullscreen MPV display
Path=$SCRIPT_DIR
Exec=env TIME_VOLUME_DISPLAY_BACKEND=mpv $SCRIPT_DIR/start_time_volume_display.sh
Terminal=false
Categories=AudioVideo;Graphics;
EOF

chmod +x \
    "$DESKTOP_DIR/Start Time Volume.desktop" \
    "$DESKTOP_DIR/Stop Time Volume.desktop" \
    "$DESKTOP_DIR/Start Time Volume Display.desktop"

if [[ "$ENABLE_AUTOSTART" == "1" ]]; then
    cat > "$AUTOSTART_DIR/Start Time Volume.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Start Time Volume
Comment=Start the Time Volume server and MPV fullscreen display after desktop login
Path=$SCRIPT_DIR
Exec=env $START_ENV_ARGS $SCRIPT_DIR/start_time_volume_autostart.sh
Terminal=false
Categories=AudioVideo;Graphics;
X-GNOME-Autostart-enabled=true
EOF
    chmod +x "$AUTOSTART_DIR/Start Time Volume.desktop"
fi

echo "Desktop launchers created on $DESKTOP_DIR"
echo "  Start Time Volume.desktop"
echo "  Stop Time Volume.desktop"
echo "  Start Time Volume Display.desktop"
if [[ "$ENABLE_AUTOSTART" == "1" ]]; then
    echo "Autostart enabled."
    echo "Autostart log: $SCRIPT_DIR/.run/time_volume_launcher.log"
fi
