#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$SCRIPT_DIR/.run"
PID_FILE="$RUN_DIR/time_volume_server.pid"
BROWSER_PID_FILE="$RUN_DIR/time_volume_browser.pid"
MPV_PID_FILE="$RUN_DIR/time_volume_mpv.pid"
START_LOCK_FILE="$RUN_DIR/time_volume_start.lock"
LOG_FILE="$RUN_DIR/time_volume_server.log"
SERVICE_NAME="${TIME_VOLUME_SERVICE_NAME:-time-volume.service}"
MPV_START_RETRIES="${TIME_VOLUME_MPV_START_RETRIES:-30}"
MPV_START_RETRY_DELAY="${TIME_VOLUME_MPV_START_RETRY_DELAY:-1}"

HOST="${TIME_VOLUME_HOST:-0.0.0.0}"
PORT="${TIME_VOLUME_PORT:-8000}"
DISPLAY_BACKEND="${TIME_VOLUME_DISPLAY_BACKEND:-mpv}"
SERVER_DISPLAY_BACKEND="${TIME_VOLUME_SERVER_DISPLAY_BACKEND:-none}"
DISPLAY_PATH="${TIME_VOLUME_DISPLAY_PATH:-/display}"
AUTO_START_CYCLE="${TIME_VOLUME_AUTO_START_CYCLE:-}"
AUTO_START_CYCLE_DELAY_SEC="${TIME_VOLUME_AUTO_START_CYCLE_DELAY_SEC:-120}"
BOOT_SERVICE_WAIT_SECONDS="${TIME_VOLUME_BOOT_SERVICE_WAIT_SECONDS:-60}"
DEFAULT_CAMERA_URL="https://betanumeric.github.io/volumetric_time_camera/"
if [[ -n "${TIME_VOLUME_CAMERA_URL+x}" ]]; then
    CAMERA_URL="$TIME_VOLUME_CAMERA_URL"
else
    CAMERA_URL="$DEFAULT_CAMERA_URL"
fi
DISPLAY_URL="http://127.0.0.1:${PORT}${DISPLAY_PATH}"
HEALTH_URL="http://127.0.0.1:${PORT}/api/state"

mkdir -p "$RUN_DIR"

if command -v flock >/dev/null 2>&1; then
    exec 9>"$START_LOCK_FILE"
    flock 9
fi

case "$DISPLAY_BACKEND" in
    browser|mpv|none) ;;
    *)
        echo "Unknown display backend '$DISPLAY_BACKEND'; falling back to browser."
        DISPLAY_BACKEND="browser"
        ;;
esac

case "$SERVER_DISPLAY_BACKEND" in
    browser|mpv|none) ;;
    *)
        echo "Unknown server display backend '$SERVER_DISPLAY_BACKEND'; using none."
        SERVER_DISPLAY_BACKEND="none"
        ;;
esac

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

server_port_is_open() {
    python3 - "$PORT" <<'PY'
import socket
import sys

try:
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=0.5):
        raise SystemExit(0)
except Exception:
    raise SystemExit(1)
PY
}

boot_service_installed() {
    if command -v systemctl >/dev/null 2>&1; then
        local service_state
        service_state="$(systemctl is-enabled "$SERVICE_NAME" 2>/dev/null || true)"
        case "$service_state" in
            enabled|enabled-runtime|static|alias|linked|linked-runtime|indirect) return 0 ;;
            disabled|masked) return 1 ;;
        esac
    fi
    [[ -f /etc/systemd/system/time-volume.service || -f /lib/systemd/system/time-volume.service ]]
}

wait_for_existing_server() {
    local wait_seconds="$1"
    python3 - "$HEALTH_URL" "$wait_seconds" <<'PY'
import sys
import time
import urllib.request

url = sys.argv[1]
deadline = time.time() + max(0.0, float(sys.argv[2]))

while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            if response.status == 200:
                raise SystemExit(0)
    except Exception:
        time.sleep(0.5)

raise SystemExit(1)
PY
}

start_mpv_display() {
    python3 - "$PORT" <<'PY'
import json
import os
import sys
import urllib.request

port = sys.argv[1]
url = f"http://127.0.0.1:{port}/api/action"
env_keys = (
    "DBUS_SESSION_BUS_ADDRESS",
    "DESKTOP_SESSION",
    "DISPLAY",
    "HOME",
    "LANG",
    "LOGNAME",
    "PATH",
    "SHELL",
    "USER",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_CONFIG_HOME",
    "XDG_CURRENT_DESKTOP",
    "XDG_DATA_DIRS",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_DESKTOP",
    "XDG_SESSION_TYPE",
)
display_env = {}
for key in env_keys:
    value = os.environ.get(key)
    if value:
        display_env[key] = value
payload = json.dumps({
    "action": "start_display",
    "display_env": display_env,
    "force_restart": True,
}).encode("utf-8")
request = urllib.request.Request(
    url,
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)

with urllib.request.urlopen(request, timeout=15.0) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
}

start_mpv_display_with_retries() {
    local attempt=1
    while (( attempt <= MPV_START_RETRIES )); do
        if start_mpv_display; then
            return 0
        fi
        echo "MPV display start attempt ${attempt}/${MPV_START_RETRIES} failed; retrying in ${MPV_START_RETRY_DELAY}s..."
        sleep "$MPV_START_RETRY_DELAY"
        attempt=$((attempt + 1))
    done
    return 1
}

auto_start_cycle_enabled() {
    case "${AUTO_START_CYCLE,,}" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

start_cycle_countdown() {
    python3 - "$PORT" "$AUTO_START_CYCLE_DELAY_SEC" <<'PY'
import json
import sys
import urllib.request

port = sys.argv[1]
delay = sys.argv[2]
url = f"http://127.0.0.1:{port}/api/action"
payload = json.dumps({
    "action": "cycle_start_countdown",
    "seconds": delay,
    "remember_delay": True,
}).encode("utf-8")
request = urllib.request.Request(
    url,
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)

with urllib.request.urlopen(request, timeout=5.0) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
}

start_server() {
    if server_is_healthy; then
        return 0
    fi

    if boot_service_installed; then
        if wait_for_existing_server "$BOOT_SERVICE_WAIT_SECONDS"; then
            return 0
        fi
    fi

    if server_port_is_open; then
        return 0
    fi

    if [[ -f "$PID_FILE" ]]; then
        local existing_pid
        existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
            return 0
        fi
    fi

    TIME_VOLUME_AUTO_START_CYCLE=0 nohup python3 "$SCRIPT_DIR/actuator_web.py" --host "$HOST" --port "$PORT" --display-backend "$SERVER_DISPLAY_BACKEND" >>"$LOG_FILE" 2>&1 &
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
        if [[ -n "$CAMERA_URL" ]]; then
            echo "Camera    : $CAMERA_URL"
        elif [[ -f "$SCRIPT_DIR/camera_app/index.html" ]]; then
            echo "Camera    : http://${ip_address}:${PORT}/camera/"
        else
            echo "Camera    : not configured"
        fi
    fi
    echo "Display   : $DISPLAY_URL"
    echo "Backend   : $DISPLAY_BACKEND"
}

if [[ "$DISPLAY_BACKEND" == "mpv" ]] && ! command -v "${TIME_VOLUME_MPV_BIN:-mpv}" >/dev/null 2>&1; then
    echo "MPV not found; falling back to browser display."
    DISPLAY_BACKEND="browser"
fi

if [[ "$DISPLAY_BACKEND" == "mpv" ]]; then
    stop_pid_file "$BROWSER_PID_FILE"
else
    stop_pid_file "$MPV_PID_FILE"
fi

start_server

if wait_for_server; then
    if [[ "$DISPLAY_BACKEND" == "mpv" ]]; then
        if ! start_mpv_display_with_retries; then
            echo "Server is running, but MPV display could not be started. Check $LOG_FILE"
            exit 1
        fi
    elif [[ "$DISPLAY_BACKEND" != "none" ]]; then
        launch_browser
    fi
    if auto_start_cycle_enabled; then
        if start_cycle_countdown; then
            echo "Cycle auto-start countdown set for ${AUTO_START_CYCLE_DELAY_SEC}s."
        else
            echo "Server is running, but the cycle auto-start countdown could not be set."
        fi
    fi
    print_urls
else
    echo "Server failed to start. Check $LOG_FILE"
    exit 1
fi
