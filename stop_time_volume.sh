#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$SCRIPT_DIR/.run"
PID_FILE="$RUN_DIR/time_volume_server.pid"
BROWSER_PID_FILE="$RUN_DIR/time_volume_browser.pid"

stop_pid_file() {
    local pid_file="$1"
    if [[ ! -f "$pid_file" ]]; then
        return 0
    fi

    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
}

stop_pid_file "$BROWSER_PID_FILE"
stop_pid_file "$PID_FILE"

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet time-volume.service 2>/dev/null; then
    if systemctl stop time-volume.service 2>/dev/null; then
        echo "Stopped time-volume.service."
    else
        echo "time-volume.service is still running. To stop boot-managed switch control, run:"
        echo "  sudo systemctl stop time-volume.service"
    fi
fi

echo "Time Volume stopped."
