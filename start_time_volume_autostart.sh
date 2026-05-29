#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$SCRIPT_DIR/.run"
LOG_FILE="${TIME_VOLUME_LAUNCHER_LOG:-$RUN_DIR/time_volume_launcher.log}"

mkdir -p "$RUN_DIR"

graphical_session_ready() {
    if [[ -n "${WAYLAND_DISPLAY:-}" && -n "${XDG_RUNTIME_DIR:-}" && -e "${XDG_RUNTIME_DIR}/${WAYLAND_DISPLAY}" ]]; then
        return 0
    fi
    if [[ -n "${DISPLAY:-}" ]]; then
        return 0
    fi
    return 1
}

wait_for_graphical_session() {
    local deadline=$((SECONDS + ${TIME_VOLUME_DESKTOP_WAIT_SECONDS:-45}))
    while (( SECONDS < deadline )); do
        if graphical_session_ready; then
            return 0
        fi
        sleep 1
    done
    return 1
}

{
    echo
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Time Volume desktop autostart"
    echo "DISPLAY=${DISPLAY:-}"
    echo "WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"
    echo "XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-}"

    if wait_for_graphical_session; then
        echo "Graphical session detected."
    else
        echo "Graphical session was not detected before timeout; trying MPV anyway."
    fi

    export TIME_VOLUME_DISPLAY_BACKEND="${TIME_VOLUME_DISPLAY_BACKEND:-mpv}"
    export TIME_VOLUME_MPV_START_RETRIES="${TIME_VOLUME_MPV_START_RETRIES:-45}"
    export TIME_VOLUME_MPV_START_RETRY_DELAY="${TIME_VOLUME_MPV_START_RETRY_DELAY:-1}"
    export TIME_VOLUME_BOOT_SERVICE_WAIT_SECONDS="${TIME_VOLUME_BOOT_SERVICE_WAIT_SECONDS:-60}"

    "$SCRIPT_DIR/start_time_volume.sh"
    status=$?
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] start_time_volume.sh exited with status $status"
    exit "$status"
} >>"$LOG_FILE" 2>&1
