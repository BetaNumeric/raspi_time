#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${TIME_VOLUME_SERVICE_NAME:-time-volume.service}"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${TIME_VOLUME_PYTHON:-$(command -v python3 || printf '/usr/bin/python3')}"
HOST="${TIME_VOLUME_HOST:-0.0.0.0}"
PORT="${TIME_VOLUME_PORT:-8000}"

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
ExecStart=$PYTHON_BIN $SCRIPT_DIR/actuator_web.py --host $HOST --port $PORT --require-gpio
Restart=always
RestartSec=2
User=$RUN_USER
Group=$RUN_GROUP
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo "Installed and started $SERVICE_NAME."
echo "Switch control will become available after the Pi has booted and this service starts."
echo "Check status with: systemctl status $SERVICE_NAME"
