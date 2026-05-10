#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$SCRIPT_DIR/.run"
LOG_FILE="${TIME_VOLUME_DISPLAY_LOG:-$RUN_DIR/time_volume_display_launcher.log}"

mkdir -p "$RUN_DIR"

{
    echo
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Time Volume display launcher"
    export TIME_VOLUME_DISPLAY_BACKEND="${TIME_VOLUME_DISPLAY_BACKEND:-mpv}"
    export TIME_VOLUME_MPV_START_RETRIES="${TIME_VOLUME_MPV_START_RETRIES:-10}"
    export TIME_VOLUME_MPV_START_RETRY_DELAY="${TIME_VOLUME_MPV_START_RETRY_DELAY:-1}"
    "$SCRIPT_DIR/start_time_volume.sh"
    status=$?
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] display launcher exited with status $status"
    exit "$status"
} >>"$LOG_FILE" 2>&1
