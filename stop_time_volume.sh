#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$SCRIPT_DIR/.run"
PID_FILE="$RUN_DIR/time_volume_server.pid"
BROWSER_PID_FILE="$RUN_DIR/time_volume_browser.pid"
MPV_PID_FILE="$RUN_DIR/time_volume_mpv.pid"
MPV_IPC_PATH="$RUN_DIR/time_volume_mpv.sock"
START_LOCK_FILE="$RUN_DIR/time_volume_start.lock"

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

stop_pid_file "$BROWSER_PID_FILE"
stop_mpv_pid_file "$MPV_PID_FILE"
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
