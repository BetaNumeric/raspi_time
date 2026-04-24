#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$SCRIPT_DIR/.run"
PID_FILE="$RUN_DIR/time_volume_server.pid"
BROWSER_PID_FILE="$RUN_DIR/time_volume_browser.pid"
LOG_FILE="$RUN_DIR/time_volume_server.log"

HOST="${TIME_VOLUME_HOST:-0.0.0.0}"
PORT="${TIME_VOLUME_PORT:-8000}"
DISPLAY_PATH="${TIME_VOLUME_DISPLAY_PATH:-/display}"
DISPLAY_URL="http://127.0.0.1:${PORT}${DISPLAY_PATH}"
HEALTH_URL="http://127.0.0.1:${PORT}/api/state"

mkdir -p "$RUN_DIR"

find_browser() {
    for browser in chromium-browser chromium epiphany-browser epiphany firefox; do
        if command -v "$browser" >/dev/null 2>&1; then
            printf '%s\n' "$browser"
            return 0
        fi
    done
    return 1
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

start_server() {
    if server_is_healthy; then
        return 0
    fi

    if [[ -f "$PID_FILE" ]]; then
        local existing_pid
        existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
            return 0
        fi
    fi

    nohup python3 "$SCRIPT_DIR/actuator_web.py" --host "$HOST" --port "$PORT" >>"$LOG_FILE" 2>&1 &
    echo "$!" > "$PID_FILE"
}

wait_for_server() {
    python3 - "$HEALTH_URL" <<'PY'
import sys
import time
import urllib.request

url = sys.argv[1]
deadline = time.time() + 20.0

while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            if response.status == 200:
                raise SystemExit(0)
    except Exception:
        time.sleep(0.4)

raise SystemExit(1)
PY
}

launch_browser() {
    local browser
    browser="$(find_browser)" || {
        echo "No supported browser found. Server is running at $DISPLAY_URL"
        return 0
    }

    if [[ -f "$BROWSER_PID_FILE" ]]; then
        local existing_browser_pid
        existing_browser_pid="$(cat "$BROWSER_PID_FILE" 2>/dev/null || true)"
        if [[ -n "$existing_browser_pid" ]] && kill -0 "$existing_browser_pid" 2>/dev/null; then
            kill "$existing_browser_pid" 2>/dev/null || true
            sleep 1
        fi
    fi

    case "$browser" in
        chromium-browser|chromium)
            "$browser" --new-window --kiosk --app="$DISPLAY_URL" >/dev/null 2>&1 &
            ;;
        epiphany-browser|epiphany)
            "$browser" --application-mode "$DISPLAY_URL" >/dev/null 2>&1 &
            ;;
        firefox)
            "$browser" --kiosk "$DISPLAY_URL" >/dev/null 2>&1 &
            ;;
    esac

    echo "$!" > "$BROWSER_PID_FILE"
}

print_urls() {
    local ip_address
    ip_address="$(hostname -I 2>/dev/null | awk '{print $1}')"
    if [[ -n "${ip_address:-}" ]]; then
        echo "Controller: http://${ip_address}:${PORT}/controller"
        echo "Camera    : http://${ip_address}:${PORT}/camera/"
    fi
    echo "Display   : $DISPLAY_URL"
}

start_server

if wait_for_server; then
    launch_browser
    print_urls
else
    echo "Server failed to start. Check $LOG_FILE"
    exit 1
fi
