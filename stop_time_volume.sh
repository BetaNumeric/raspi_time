#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$SCRIPT_DIR/.run"
PID_FILE="$RUN_DIR/time_volume_server.pid"
BROWSER_PID_FILE="$RUN_DIR/time_volume_browser.pid"
MPV_PID_FILE="$RUN_DIR/time_volume_mpv.pid"
MPV_IPC_PATH="$RUN_DIR/time_volume_mpv.sock"
START_LOCK_FILE="$RUN_DIR/time_volume_start.lock"
SERVICE_NAME="${TIME_VOLUME_SERVICE_NAME:-time-volume.service}"
PORT="${TIME_VOLUME_PORT:-8000}"
HEALTH_URL="http://127.0.0.1:${PORT}/api/state"
ACTION_URL="http://127.0.0.1:${PORT}/api/action"
STOP_SERVER="${TIME_VOLUME_STOP_SERVER:-0}"
STOP_SERVICE="${TIME_VOLUME_STOP_SERVICE:-0}"

mkdir -p "$RUN_DIR"

if command -v flock >/dev/null 2>&1; then
    exec 9>"$START_LOCK_FILE"
    flock 9
fi

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

truthy() {
    case "${1,,}" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

server_is_healthy() {
    python3 - "$HEALTH_URL" <<'PY'
import sys
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=1.5) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
}

post_action() {
    local action="$1"
    python3 - "$ACTION_URL" "$action" <<'PY'
import json
import sys
import urllib.request

url = sys.argv[1]
action = sys.argv[2]
payload = json.dumps({"action": action}).encode("utf-8")
request = urllib.request.Request(
    url,
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)

with urllib.request.urlopen(request, timeout=5.0) as response:
    raise SystemExit(0 if response.status == 200 else 1)
PY
}

service_is_active() {
    command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null
}

stop_service() {
    if command -v systemctl >/dev/null 2>&1 && systemctl stop "$SERVICE_NAME" 2>/dev/null; then
        echo "Stopped $SERVICE_NAME."
        return 0
    fi
    echo "$SERVICE_NAME is still running or could not be stopped without elevated permissions."
    echo "To stop boot-managed switch control, run:"
    echo "  sudo systemctl stop $SERVICE_NAME"
    return 1
}

stop_mpv_pid_file() {
    local pid_file="$1"
    local pid=""
    if [[ -f "$pid_file" ]]; then
        pid="$(cat "$pid_file" 2>/dev/null || true)"
    fi

    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        local cmdline=""
        if [[ -r "/proc/$pid/cmdline" ]]; then
            cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
        fi
        if [[ "$cmdline" == *"$MPV_IPC_PATH"* ]]; then
            kill "$pid" 2>/dev/null || true
        fi
    fi

    for cmdline_path in /proc/[0-9]*/cmdline; do
        [[ -r "$cmdline_path" ]] || continue
        local cmdline=""
        cmdline="$(tr '\0' ' ' <"$cmdline_path" 2>/dev/null || true)"
        if [[ "$cmdline" == *"$MPV_IPC_PATH"* ]]; then
            local proc_dir
            proc_dir="$(dirname "$cmdline_path")"
            kill "${proc_dir##*/}" 2>/dev/null || true
        fi
    done

    rm -f "$pid_file"
}

if server_is_healthy; then
    post_action stop || true
    post_action stop_display || true
fi

stop_pid_file "$BROWSER_PID_FILE"
stop_mpv_pid_file "$MPV_PID_FILE"

if truthy "$STOP_SERVICE"; then
    stop_service || true
    stop_pid_file "$PID_FILE"
elif truthy "$STOP_SERVER"; then
    stop_pid_file "$PID_FILE"
elif service_is_active; then
    rm -f "$PID_FILE"
else
    stop_pid_file "$PID_FILE"
fi

echo "Time Volume stopped."
