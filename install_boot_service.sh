#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${TIME_VOLUME_SERVICE_NAME:-time-volume.service}"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${TIME_VOLUME_PYTHON:-$(command -v python3 || printf '/usr/bin/python3')}"
HOST="${TIME_VOLUME_HOST:-0.0.0.0}"
PORT="${TIME_VOLUME_PORT:-8000}"
CAMERA_URL="${TIME_VOLUME_CAMERA_URL:-}"
CAMERA_DIR="${TIME_VOLUME_CAMERA_DIR:-}"
DISPLAY_BACKEND="${TIME_VOLUME_DISPLAY_BACKEND:-none}"
SERVICE_AUTO_START_CYCLE="${TIME_VOLUME_SERVICE_AUTO_START_CYCLE:-}"
SERVICE_AUTO_START_CYCLE_DELAY_SEC="${TIME_VOLUME_SERVICE_AUTO_START_CYCLE_DELAY_SEC:-}"

case "$DISPLAY_BACKEND" in
    browser|mpv|none) ;;
    *)
        echo "Unknown display backend '$DISPLAY_BACKEND'; using none for boot service."
        DISPLAY_BACKEND="none"
        ;;
esac

if [[ "${1:-}" == "--uninstall" ]]; then
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "Run with sudo to uninstall the boot service:"
        echo "  sudo $0 --uninstall"
        exit 1
    fi

    systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$SERVICE_PATH"
    systemctl daemon-reload
    echo "Removed $SERVICE_NAME."
    exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run with sudo to install the boot service:"
    echo "  sudo $0"
    exit 1
fi

RUN_USER="${TIME_VOLUME_USER:-${SUDO_USER:-$(logname 2>/dev/null || echo root)}}"
RUN_GROUP="${TIME_VOLUME_GROUP:-$(id -gn "$RUN_USER" 2>/dev/null || echo "$RUN_USER")}"

cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=Time Volume actuator server
After=local-fs.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON_BIN $SCRIPT_DIR/actuator_web.py --host $HOST --port $PORT --require-gpio --display-backend $DISPLAY_BACKEND
Restart=always
RestartSec=2
User=$RUN_USER
Group=$RUN_GROUP
Environment=PYTHONUNBUFFERED=1
EOF

if [[ -n "$CAMERA_URL" ]]; then
    printf 'Environment="TIME_VOLUME_CAMERA_URL=%s"\n' "$CAMERA_URL" >> "$SERVICE_PATH"
fi

if [[ -n "$CAMERA_DIR" ]]; then
    printf 'Environment="TIME_VOLUME_CAMERA_DIR=%s"\n' "$CAMERA_DIR" >> "$SERVICE_PATH"
fi

if [[ -n "$SERVICE_AUTO_START_CYCLE" ]]; then
    printf 'Environment="TIME_VOLUME_AUTO_START_CYCLE=%s"\n' "$SERVICE_AUTO_START_CYCLE" >> "$SERVICE_PATH"
fi

if [[ -n "$SERVICE_AUTO_START_CYCLE_DELAY_SEC" ]]; then
    printf 'Environment="TIME_VOLUME_AUTO_START_CYCLE_DELAY_SEC=%s"\n' "$SERVICE_AUTO_START_CYCLE_DELAY_SEC" >> "$SERVICE_PATH"
fi

cat >> "$SERVICE_PATH" <<EOF
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo "Installed and started $SERVICE_NAME."
echo "Switch control will become available after the Pi has booted and this service starts."
echo "Check status with: systemctl status $SERVICE_NAME"
if [[ -n "${TIME_VOLUME_AUTO_START_CYCLE:-}" || -n "${TIME_VOLUME_AUTO_START_CYCLE_DELAY_SEC:-}" ]]; then
    echo "Note: TIME_VOLUME_AUTO_START_CYCLE is ignored by the boot service installer."
    echo "Use install_pi_launchers.sh --autostart for the 120s display-ready cycle countdown."
fi
