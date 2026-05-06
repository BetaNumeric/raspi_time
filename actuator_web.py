#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import random
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    import gpiod  # type: ignore
except Exception:  # pragma: no cover
    gpiod = None

try:
    from experiments.pi_long_exposure.pi_camera_runtime import (
        CameraBusyError,
        CameraUnavailableError,
        PiCameraRuntime,
        RecordSettings,
    )
except Exception as exc:  # pragma: no cover - optional experiment may be removed
    CameraBusyError = RuntimeError  # type: ignore
    CameraUnavailableError = RuntimeError  # type: ignore
    PiCameraRuntime = None  # type: ignore
    RecordSettings = None  # type: ignore
    PI_CAMERA_IMPORT_ERROR = str(exc)
else:
    PI_CAMERA_IMPORT_ERROR = None


CHIPPATH = "/dev/gpiochip0"
RPWM = 18
LPWM = 19
REN = 23
LEN = 24
PWM_HZ = 200
EXTEND_SEC = 23.75
RETRACT_SEC = 22.0
HOME_TIMEOUT_SEC = 25.0
LIFT_STROKE_CM = 48.0
MIN_SEQUENCE_HEIGHT_CM = 0.01

SW_EXTEND_IN = 6
SW_RETRACT_IN = 5
SW_ACTIVE_LOW = True
SW_POLL_MS = 30
SW_DEBOUNCE_SAMPLES = 2

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
MEDIA_ROOT = BASE_DIR / "media"
RUN_DIR = BASE_DIR / ".run"
CAMERA_APP_DIR = Path(os.environ.get("TIME_VOLUME_CAMERA_DIR", BASE_DIR / "camera_app")).expanduser()
if not CAMERA_APP_DIR.is_absolute():
    CAMERA_APP_DIR = BASE_DIR / CAMERA_APP_DIR
CAMERA_APP_DIR = CAMERA_APP_DIR.resolve()
DEFAULT_CAMERA_URL = "https://betanumeric.github.io/volumetric_time_camera/"
EXTERNAL_CAMERA_URL = os.environ.get("TIME_VOLUME_CAMERA_URL", DEFAULT_CAMERA_URL).strip()
STATE_FILE = BASE_DIR / "actuator_state.json"
DEFAULT_HOST = os.environ.get("ACTUATOR_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("ACTUATOR_PORT", "8000"))

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
SEQUENCE_METADATA_FILENAME = "sequence.json"
OUTSIDE_PLAYBACK_MODES = {"black", "hold"}
MPV_DEFAULT_FPS_CAP = 30.0
DEFAULT_DISPLAY_BACKEND = "mpv"
MPV_IPC_PATH = RUN_DIR / "time_volume_mpv.sock"
MPV_PID_FILE = RUN_DIR / "time_volume_mpv.pid"
MPV_BLACK_IMAGE = RUN_DIR / "mpv_black.png"


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def coerce_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if math.isfinite(parsed) else fallback


def sequence_span_cm(sequence: "SequenceItem") -> tuple[float, float, float]:
    start_cm = clamp(sequence.start_cm, 0.0, LIFT_STROKE_CM - MIN_SEQUENCE_HEIGHT_CM)
    end_cm = clamp(sequence.end_cm, start_cm + MIN_SEQUENCE_HEIGHT_CM, LIFT_STROKE_CM)
    return start_cm, end_cm, end_cm - start_cm


def sequence_travel_duration_sec(sequence: "SequenceItem", direction: int, duty: float) -> float:
    _, _, height_cm = sequence_span_cm(sequence)
    stroke_sec = EXTEND_SEC if direction >= 0 else RETRACT_SEC
    effective_duty = clamp(duty, 0.1, 1.0)
    return ((height_cm / LIFT_STROKE_CM) * stroke_sec) / effective_duty


def frame_stride_for_sequence(sequence: "SequenceItem", direction: int, duty: float, fps_cap: float) -> tuple[int, float, float]:
    if sequence.kind != "images" or sequence.frame_count <= 1:
        return 1, 0.0, 0.0
    duration_sec = max(0.01, sequence_travel_duration_sec(sequence, direction, duty))
    requested_fps = (sequence.frame_count - 1) / duration_sec
    stride = max(1, math.ceil(requested_fps / max(1.0, fps_cap)))
    return stride, requested_fps, duration_sec


def media_url_for(relative_path: str) -> str:
    return f"/media/{quote(relative_path, safe='/')}"


def camera_app_available() -> bool:
    return (CAMERA_APP_DIR / "index.html").is_file()


def configured_camera_url() -> str | None:
    if EXTERNAL_CAMERA_URL:
        return EXTERNAL_CAMERA_URL
    if camera_app_available():
        return "/camera/"
    return None


@dataclass
class SequenceItem:
    id: str
    name: str
    kind: str
    relative_path: str
    frame_count: int = 0
    frame_paths: list[str] = field(default_factory=list)
    media_url: str | None = None
    start_cm: float = 0.0
    volume_height_cm: float = LIFT_STROKE_CM
    outside_playback: str = "black"

    @property
    def end_cm(self) -> float:
        return min(LIFT_STROKE_CM, self.start_cm + self.volume_height_cm)

    def playback_ratio_for_pct(self, position_pct: float) -> float | None:
        position_cm = (clamp(position_pct, 0.0, 100.0) / 100.0) * LIFT_STROKE_CM
        end_cm = self.end_cm

        if position_cm < self.start_cm:
            return 0.0 if self.outside_playback == "hold" else None
        if position_cm > end_cm:
            return 1.0 if self.outside_playback == "hold" else None

        span_cm = max(MIN_SEQUENCE_HEIGHT_CM, end_cm - self.start_cm)
        return clamp((position_cm - self.start_cm) / span_cm, 0.0, 1.0)

    def frame_index_for_pct(self, position_pct: float) -> int | None:
        ratio = self.playback_ratio_for_pct(position_pct)
        if ratio is None:
            return None
        if self.kind != "images" or self.frame_count <= 1:
            return 0
        return min(self.frame_count - 1, int(round(ratio * (self.frame_count - 1))))

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "relative_path": self.relative_path,
            "frame_count": self.frame_count,
            "media_url": self.media_url,
            "lift_stroke_cm": LIFT_STROKE_CM,
            "start_cm": round(self.start_cm, 3),
            "end_cm": round(self.end_cm, 3),
            "volume_height_cm": round(self.volume_height_cm, 3),
            "outside_playback": self.outside_playback,
        }

    def detail(self) -> dict[str, Any]:
        payload = self.summary()
        payload["frame_urls"] = [media_url_for(path) for path in self.frame_paths]
        return payload


class SequenceLibrary:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._items: dict[str, SequenceItem] = {}
        self._order: list[str] = []
        self.scan()

    def _read_metadata(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"Warning: could not read sequence metadata {path}: {exc}")
            return {}
        if not isinstance(payload, dict):
            print(f"Warning: sequence metadata {path} must be a JSON object.")
            return {}
        return payload

    def _metadata_for_directory(self, directory: Path) -> dict[str, Any]:
        return self._read_metadata(directory / SEQUENCE_METADATA_FILENAME)

    def _metadata_for_video(self, file_path: Path) -> dict[str, Any]:
        return self._read_metadata(file_path.with_suffix(".json"))

    def _sequence_span(self, metadata: dict[str, Any]) -> tuple[float, float, str]:
        height_value = metadata.get(
            "volume_height_cm",
            metadata.get("height_cm", metadata.get("length_cm", LIFT_STROKE_CM)),
        )
        start_value = metadata.get("start_cm", metadata.get("offset_cm", 0.0))

        height_cm = clamp(
            coerce_float(height_value, LIFT_STROKE_CM),
            MIN_SEQUENCE_HEIGHT_CM,
            LIFT_STROKE_CM,
        )
        start_cm = clamp(
            coerce_float(start_value, 0.0),
            0.0,
            max(0.0, LIFT_STROKE_CM - MIN_SEQUENCE_HEIGHT_CM),
        )
        if start_cm + height_cm > LIFT_STROKE_CM:
            height_cm = max(MIN_SEQUENCE_HEIGHT_CM, LIFT_STROKE_CM - start_cm)

        outside_playback = str(metadata.get("outside_playback", "black")).strip().lower()
        if outside_playback not in OUTSIDE_PLAYBACK_MODES:
            outside_playback = "black"

        return start_cm, height_cm, outside_playback

    def scan(self) -> list[dict[str, Any]]:
        root_resolved = self.root.resolve()
        items: dict[str, SequenceItem] = {}

        for current_dir, dirnames, filenames in os.walk(self.root):
            dirnames.sort()
            directory = Path(current_dir)
            directory_resolved = directory.resolve()
            rel_dir = "" if directory_resolved == root_resolved else directory_resolved.relative_to(root_resolved).as_posix()
            image_paths: list[str] = []

            for name in sorted(filenames):
                file_path = directory / name
                suffix = file_path.suffix.lower()
                rel_path = file_path.relative_to(self.root).as_posix()

                if suffix in VIDEO_EXTENSIONS:
                    start_cm, height_cm, outside_playback = self._sequence_span(
                        self._metadata_for_video(file_path)
                    )
                    items[rel_path] = SequenceItem(
                        id=rel_path,
                        name=file_path.stem,
                        kind="video",
                        relative_path=rel_path,
                        media_url=media_url_for(rel_path),
                        start_cm=start_cm,
                        volume_height_cm=height_cm,
                        outside_playback=outside_playback,
                    )
                elif suffix in IMAGE_EXTENSIONS:
                    image_paths.append(rel_path)

            if not image_paths:
                continue

            sequence_id = rel_dir or "__root__"
            display_name = directory.name if rel_dir else "media"
            start_cm, height_cm, outside_playback = self._sequence_span(
                self._metadata_for_directory(directory)
            )
            items[sequence_id] = SequenceItem(
                id=sequence_id,
                name=display_name,
                kind="images",
                relative_path=rel_dir or ".",
                frame_count=len(image_paths),
                frame_paths=image_paths,
                start_cm=start_cm,
                volume_height_cm=height_cm,
                outside_playback=outside_playback,
            )

        order = sorted(
            items,
            key=lambda item_id: (
                items[item_id].kind != "images",
                items[item_id].name.lower(),
                item_id.lower(),
            ),
        )

        with self._lock:
            self._items = items
            self._order = order

        return self.list_summaries()

    def list_summaries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._items[item_id].summary() for item_id in self._order]

    def ordered_ids(self) -> list[str]:
        with self._lock:
            return list(self._order)

    def get(self, item_id: str | None) -> SequenceItem | None:
        if not item_id:
            return None
        with self._lock:
            return self._items.get(item_id)


class ActuatorHardware:
    def __init__(self, req: Any):
        self.req = req
        self.position_pct = 0.0
        self.is_moving = False
        self.is_paused = False
        self.current_duty = 1.0
        self.lock = threading.Lock()
        self.io_lock = threading.Lock()
        self.motion_stop_event = threading.Event()
        self.motion_token = 0

    def _set_line_value(self, line: int, value: Any, ignore_errors: bool = False) -> None:
        if not self.req or gpiod is None:
            return
        try:
            with self.io_lock:
                self.req.set_value(line, value)
        except Exception:
            if ignore_errors:
                return
            raise

    def _get_line_value(self, line: int) -> Any:
        if not self.req or gpiod is None:
            return None
        with self.io_lock:
            return self.req.get_value(line)

    def _deactivate_outputs(self, ignore_errors: bool = False) -> None:
        if not self.req or gpiod is None:
            return
        self._set_line_value(RPWM, gpiod.line.Value.INACTIVE, ignore_errors=ignore_errors)
        self._set_line_value(LPWM, gpiod.line.Value.INACTIVE, ignore_errors=ignore_errors)

    def _begin_motion_session(self, clear_paused: bool = False) -> tuple[threading.Event, int]:
        with self.lock:
            self.motion_stop_event.set()
            self.motion_token += 1
            token = self.motion_token
            stop_event = threading.Event()
            self.motion_stop_event = stop_event
            self.is_moving = True
            if clear_paused:
                self.is_paused = False
        return stop_event, token

    def _finish_motion_session(self, token: int, stop_event: threading.Event) -> bool:
        with self.lock:
            if token != self.motion_token or self.motion_stop_event is not stop_event:
                return False
            self.is_moving = False
            return True

    def enable(self, on: bool = True, ignore_errors: bool = False) -> None:
        if not self.req or gpiod is None:
            return
        value = gpiod.line.Value.ACTIVE if on else gpiod.line.Value.INACTIVE
        self._set_line_value(REN, value, ignore_errors=ignore_errors)
        self._set_line_value(LEN, value, ignore_errors=ignore_errors)

    def stop_pwm(self, ignore_errors: bool = False) -> None:
        with self.lock:
            self.motion_stop_event.set()
        self._deactivate_outputs(ignore_errors=ignore_errors)

    def pause(self) -> None:
        with self.lock:
            self.is_paused = True
        self.stop_pwm()

    def resume(self) -> None:
        with self.lock:
            self.is_paused = False

    def _soft_pwm_loop(
        self,
        pin: int,
        duty: float,
        duration: float,
        update_callback: Any = None,
        stop_condition: Any = None,
    ) -> None:
        stop_event, motion_token = self._begin_motion_session()
        self.enable(True)

        other_pin = LPWM if pin == RPWM else RPWM
        if self.req and gpiod is not None:
            self._set_line_value(other_pin, gpiod.line.Value.INACTIVE)

        period = 1.0 / PWM_HZ
        start_time = time.time()
        last_tick = start_time

        try:
            while time.time() - start_time < duration:
                if stop_event.is_set():
                    break

                effective_duty = clamp(self.current_duty, 0.0, 1.0)
                t_on = period * effective_duty
                t_off = period - t_on

                if self.req and gpiod is not None:
                    if t_on > 0:
                        self._set_line_value(pin, gpiod.line.Value.ACTIVE)
                        time.sleep(t_on)
                    self._set_line_value(pin, gpiod.line.Value.INACTIVE)
                    if t_off > 0:
                        time.sleep(t_off)
                else:
                    time.sleep(period)

                if stop_event.is_set():
                    break

                now = time.time()
                dt = now - last_tick
                last_tick = now

                if update_callback:
                    update_callback(dt)

                if stop_condition and stop_condition():
                    break
        finally:
            if self._finish_motion_session(motion_token, stop_event):
                self._deactivate_outputs(ignore_errors=True)
                self.enable(False, ignore_errors=True)

    def move_diff(self, delta_pct: float, duty: float = 1.0) -> None:
        if abs(delta_pct) < 0.01:
            return

        with self.lock:
            start_pct = self.position_pct
        target_pct = clamp(start_pct + delta_pct, 0.0, 100.0)

        pin = RPWM if delta_pct > 0 else LPWM
        direction = 1 if delta_pct > 0 else -1
        stroke_sec = EXTEND_SEC if delta_pct > 0 else RETRACT_SEC

        def update_position(dt: float) -> None:
            effective_duty = max(0.1, self.current_duty)
            speed_pct_per_sec = (100.0 / stroke_sec) * effective_duty * direction
            with self.lock:
                self.position_pct = clamp(self.position_pct + speed_pct_per_sec * dt, 0.0, 100.0)

        def stop_condition() -> bool:
            with self.lock:
                current = self.position_pct
            if direction > 0:
                return current >= target_pct - 0.5
            return current <= target_pct + 0.5

        self._soft_pwm_loop(pin, duty, 3600.0, update_position, stop_condition)

    def manual_move(self, direction: int, duty: float = 1.0) -> None:
        if direction == 0:
            return

        pin = RPWM if direction > 0 else LPWM
        stroke_sec = EXTEND_SEC if direction > 0 else RETRACT_SEC

        def update_position(dt: float) -> None:
            effective_duty = max(0.1, self.current_duty)
            speed_pct_per_sec = (100.0 / stroke_sec) * effective_duty * direction
            with self.lock:
                self.position_pct = clamp(self.position_pct + speed_pct_per_sec * dt, 0.0, 100.0)

        self._soft_pwm_loop(pin, duty, 3600.0, update_position)

    def home(self) -> None:
        stop_event, motion_token = self._begin_motion_session(clear_paused=True)
        self.enable(True)

        if self.req and gpiod is not None:
            self._set_line_value(RPWM, gpiod.line.Value.INACTIVE)

        period = 1.0 / PWM_HZ
        start_time = time.time()
        reached_home = False
        try:
            while time.time() - start_time < HOME_TIMEOUT_SEC:
                if stop_event.is_set():
                    break
                if self.req and gpiod is not None:
                    self._set_line_value(LPWM, gpiod.line.Value.ACTIVE)
                    time.sleep(period)
                    self._set_line_value(LPWM, gpiod.line.Value.INACTIVE)
                else:
                    time.sleep(period)
                if stop_event.is_set():
                    break
            else:
                reached_home = True
        finally:
            if self._finish_motion_session(motion_token, stop_event):
                self._deactivate_outputs(ignore_errors=True)
                self.enable(False, ignore_errors=True)
                if reached_home:
                    with self.lock:
                        self.position_pct = 0.0


class InstallationController:
    def __init__(self, req: Any):
        self.hardware = ActuatorHardware(req)
        if req:
            self.hardware.enable(False)
            self.hardware.stop_pwm()

        self.library = SequenceLibrary(MEDIA_ROOT)
        self.state_lock = threading.Lock()
        self.settings_io_lock = threading.Lock()
        self.settings_timer_lock = threading.Lock()
        self.command_lock = threading.Lock()
        self.shutdown_event = threading.Event()

        self.target_pct = 0.0
        self.delay_sec = 0
        self.speed_duty = 1.0
        self.cycle_pause_extend_sec = 2.0
        self.cycle_pause_retract_sec = 2.0
        self.cycle_random = False
        self.selected_sequence_id: str | None = None
        self.is_cycling = False
        self.is_cycle_paused = False
        self.cycle_token = 0
        self.cycle_thread: threading.Thread | None = None
        self.active_target: float | None = None
        self.movement_token = 0

        self.countdown_deadline: float | None = None
        self.countdown_event: threading.Event | None = None
        self.cycle_pause_phase: str | None = None
        self.cycle_pause_deadline: float | None = None
        self.last_cycle_end_reason: str | None = None
        self.last_runtime_error: str | None = None
        self.settings_save_timer: threading.Timer | None = None
        self.display_backend_mode = "browser"
        self.display_backend_status = "HTML display fallback"

        self.switch_raw_dir = 0
        self.switch_stable_dir = 0
        self.switch_same_count = 0

        self._load_settings()
        self._ensure_valid_selection()
        self.hardware.current_duty = self.speed_duty

        self.switch_thread = threading.Thread(target=self._poll_toggle_switch_loop, daemon=True)
        self.switch_thread.start()

    def _load_settings(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return

        self.target_pct = clamp(float(data.get("target_pct", self.target_pct)), 0.0, 100.0)
        self.delay_sec = max(0, int(data.get("delay_sec", self.delay_sec)))
        self.speed_duty = clamp(float(data.get("speed_duty", self.speed_duty)), 0.1, 1.0)
        self.cycle_pause_extend_sec = max(0.0, float(data.get("cycle_pause_extend_sec", self.cycle_pause_extend_sec)))
        self.cycle_pause_retract_sec = max(0.0, float(data.get("cycle_pause_retract_sec", self.cycle_pause_retract_sec)))
        self.cycle_random = bool(data.get("cycle_random", self.cycle_random))
        selected = data.get("selected_sequence_id")
        self.selected_sequence_id = selected if isinstance(selected, str) else None

    def _save_settings(self) -> None:
        with self.state_lock:
            payload = {
                "target_pct": round(self.target_pct, 2),
                "delay_sec": self.delay_sec,
                "speed_duty": round(self.speed_duty, 3),
                "cycle_pause_extend_sec": round(self.cycle_pause_extend_sec, 2),
                "cycle_pause_retract_sec": round(self.cycle_pause_retract_sec, 2),
                "cycle_random": self.cycle_random,
                "selected_sequence_id": self.selected_sequence_id,
            }
        temp_path = STATE_FILE.with_suffix(".tmp")
        body = json.dumps(payload, indent=2)
        with self.settings_io_lock:
            try:
                temp_path.write_text(body, encoding="utf-8")
                temp_path.replace(STATE_FILE)
                return
            except Exception as exc:
                print(f"Warning: atomic state save failed ({exc}). Falling back to direct write.")
                try:
                    STATE_FILE.write_text(body, encoding="utf-8")
                    return
                except Exception as fallback_exc:
                    print(f"Warning: state save failed ({fallback_exc}). Continuing without persistence.")
                finally:
                    try:
                        if temp_path.exists():
                            temp_path.unlink()
                    except Exception:
                        pass

    def _flush_scheduled_settings_save(self) -> None:
        with self.settings_timer_lock:
            self.settings_save_timer = None
        self._save_settings()

    def _schedule_settings_save(self, delay: float = 0.5) -> None:
        with self.settings_timer_lock:
            if self.settings_save_timer:
                self.settings_save_timer.cancel()
            self.settings_save_timer = threading.Timer(delay, self._flush_scheduled_settings_save)
            self.settings_save_timer.daemon = True
            self.settings_save_timer.start()

    def _cancel_scheduled_settings_save(self) -> None:
        with self.settings_timer_lock:
            timer = self.settings_save_timer
            self.settings_save_timer = None
        if timer:
            timer.cancel()

    def _ensure_valid_selection(self) -> None:
        ordered = self.library.ordered_ids()
        with self.state_lock:
            if self.selected_sequence_id in ordered:
                return
            self.selected_sequence_id = ordered[0] if ordered else None

    def get_library(self) -> list[dict[str, Any]]:
        return self.library.list_summaries()

    def get_sequence_detail(self, sequence_id: str | None) -> dict[str, Any] | None:
        sequence = self.library.get(sequence_id)
        return sequence.detail() if sequence else None

    def refresh_library(self) -> list[dict[str, Any]]:
        items = self.library.scan()
        self._ensure_valid_selection()
        self._save_settings()
        return items

    def _switch_label(self, direction: int) -> str:
        if direction > 0:
            return "EXTEND"
        if direction < 0:
            return "RETRACT"
        return "OFF"

    def set_display_backend_status(self, mode: str, status: str) -> None:
        with self.state_lock:
            self.display_backend_mode = mode
            self.display_backend_status = status

    def get_state(self) -> dict[str, Any]:
        with self.hardware.lock:
            position_pct = round(self.hardware.position_pct, 2)
            is_moving = self.hardware.is_moving
            is_paused = self.hardware.is_paused

        with self.state_lock:
            countdown_deadline = self.countdown_deadline
            selected_sequence_id = self.selected_sequence_id
            target_pct = round(self.target_pct, 2)
            speed_duty = round(self.speed_duty, 3)
            delay_sec = self.delay_sec
            cycle_pause_extend_sec = round(self.cycle_pause_extend_sec, 2)
            cycle_pause_retract_sec = round(self.cycle_pause_retract_sec, 2)
            cycle_random = self.cycle_random
            is_cycling = self.is_cycling
            is_cycle_paused = self.is_cycle_paused
            active_target = self.active_target
            switch_direction = self.switch_stable_dir
            cycle_pause_phase = self.cycle_pause_phase
            cycle_pause_deadline = self.cycle_pause_deadline
            last_cycle_end_reason = self.last_cycle_end_reason
            last_runtime_error = self.last_runtime_error
            display_backend_mode = self.display_backend_mode
            display_backend_status = self.display_backend_status

        now = time.time()
        countdown_remaining = 0
        deadline_ms = None
        if countdown_deadline is not None:
            remaining = max(0.0, countdown_deadline - now)
            countdown_remaining = int(math.ceil(remaining))
            deadline_ms = int(countdown_deadline * 1000)

        cycle_pause_deadline_ms = None
        cycle_pause_remaining_ms = 0
        if cycle_pause_phase and cycle_pause_deadline is not None:
            cycle_pause_deadline_ms = int(cycle_pause_deadline * 1000)
            cycle_pause_remaining_ms = max(0, int((cycle_pause_deadline - now) * 1000))

        sequence = self.library.get(selected_sequence_id)
        return {
            "server_time_ms": int(now * 1000),
            "countdown_deadline_ms": deadline_ms,
            "countdown_remaining": countdown_remaining,
            "cycle_pause_phase": cycle_pause_phase,
            "cycle_pause_deadline_ms": cycle_pause_deadline_ms,
            "cycle_pause_remaining_ms": cycle_pause_remaining_ms,
            "last_cycle_end_reason": last_cycle_end_reason,
            "position_pct": position_pct,
            "target_pct": target_pct,
            "speed_duty": speed_duty,
            "delay_sec": delay_sec,
            "cycle_pause_extend_sec": cycle_pause_extend_sec,
            "cycle_pause_retract_sec": cycle_pause_retract_sec,
            "cycle_random": cycle_random,
            "is_moving": is_moving,
            "is_paused": is_paused,
            "is_cycling": is_cycling,
            "is_cycle_paused": is_cycle_paused,
            "active_target_pct": active_target,
            "switch_direction": switch_direction,
            "switch_label": self._switch_label(switch_direction),
            "last_runtime_error": last_runtime_error,
            "selected_sequence_id": selected_sequence_id,
            "selected_sequence_name": sequence.name if sequence else None,
            "selected_sequence_kind": sequence.kind if sequence else None,
            "selected_sequence_frame_count": sequence.frame_count if sequence else 0,
            "selected_sequence_media_url": sequence.media_url if sequence else None,
            "selected_sequence_start_cm": round(sequence.start_cm, 3) if sequence else 0.0,
            "selected_sequence_end_cm": round(sequence.end_cm, 3) if sequence else LIFT_STROKE_CM,
            "selected_sequence_volume_height_cm": round(sequence.volume_height_cm, 3) if sequence else LIFT_STROKE_CM,
            "selected_sequence_outside_playback": sequence.outside_playback if sequence else "black",
            "lift_stroke_cm": LIFT_STROKE_CM,
            "extend_sec": EXTEND_SEC,
            "retract_sec": RETRACT_SEC,
            "frame_index": sequence.frame_index_for_pct(position_pct) if sequence else 0,
            "is_simulation": self.hardware.req is None,
            "media_root": str(MEDIA_ROOT),
            "controller_url": "/controller",
            "display_url": "/display",
            "display_backend_mode": display_backend_mode,
            "display_backend_status": display_backend_status,
            "camera_url": configured_camera_url(),
        }

    def set_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        restart_target = None
        with self.command_lock:
            with self.state_lock:
                if "target_pct" in payload:
                    self.target_pct = clamp(float(payload["target_pct"]), 0.0, 100.0)
                    restart_target = self.target_pct
                if "delay_sec" in payload:
                    self.delay_sec = max(0, int(payload["delay_sec"]))
                if "speed_duty" in payload:
                    self.speed_duty = clamp(float(payload["speed_duty"]), 0.1, 1.0)
                    self.hardware.current_duty = self.speed_duty
                if "cycle_pause_extend_sec" in payload:
                    self.cycle_pause_extend_sec = max(0.0, float(payload["cycle_pause_extend_sec"]))
                if "cycle_pause_retract_sec" in payload:
                    self.cycle_pause_retract_sec = max(0.0, float(payload["cycle_pause_retract_sec"]))
                if "cycle_random" in payload:
                    self.cycle_random = bool(payload["cycle_random"])
                if "sequence_id" in payload:
                    sequence_id = payload["sequence_id"] or None
                    if sequence_id is not None and not self.library.get(str(sequence_id)):
                        raise ValueError("Unknown sequence_id")
                    self.selected_sequence_id = str(sequence_id) if sequence_id is not None else None

            if restart_target is not None and not self.is_switch_override_active() and not self.is_counting_down():
                with self.hardware.lock:
                    should_restart = self.hardware.is_moving and not self.hardware.is_paused
                if should_restart and self.active_target is not None:
                    self.restart_movement(restart_target)

            self._schedule_settings_save()

        return self.get_state()

    def _begin_target_movement(self, target_pct: float) -> int:
        with self.state_lock:
            self.movement_token += 1
            token = self.movement_token
            self.active_target = round(target_pct, 2)
        return token

    def _clear_target_movement(self, token: int) -> None:
        with self.state_lock:
            if token == self.movement_token:
                self.active_target = None

    def _move_worker(self, delta_pct: float, duty: float, token: int) -> None:
        try:
            self.hardware.move_diff(delta_pct, duty)
        except Exception as exc:
            self._set_runtime_error(str(exc))
            print(f"Movement stopped due to error: {exc}")
        finally:
            self._clear_target_movement(token)

    def _manual_move_worker(self, direction: int, duty: float, source: str = "manual") -> None:
        try:
            self.hardware.manual_move(direction, duty)
        except Exception as exc:
            self._set_runtime_error(str(exc))
            print(f"{source.capitalize()} move stopped due to error: {exc}")

    def _home_worker(self) -> None:
        try:
            self.hardware.home()
        except Exception as exc:
            self._set_runtime_error(str(exc))
            print(f"Home stopped due to error: {exc}")

    def is_counting_down(self) -> bool:
        with self.state_lock:
            return self.countdown_deadline is not None

    def start_countdown(self, seconds: int) -> None:
        self.cancel_countdown()
        if seconds <= 0:
            self.execute_movement()
            return
        event = threading.Event()
        deadline = time.time() + seconds
        with self.state_lock:
            self.countdown_deadline = deadline
            self.countdown_event = event
        threading.Thread(target=self._countdown_worker, args=(event,), daemon=True).start()

    def _countdown_worker(self, event: threading.Event) -> None:
        while not event.is_set():
            with self.state_lock:
                deadline = self.countdown_deadline
            if deadline is None:
                return
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(0.2, remaining))

        if event.is_set():
            return

        with self.state_lock:
            if self.countdown_event is event:
                self.countdown_event = None
                self.countdown_deadline = None

        self.execute_movement()

    def cancel_countdown(self) -> None:
        with self.state_lock:
            event = self.countdown_event
            self.countdown_event = None
            self.countdown_deadline = None
        if event:
            event.set()

    def _set_cycle_pause(self, phase: str, deadline: float) -> None:
        with self.state_lock:
            self.cycle_pause_phase = phase
            self.cycle_pause_deadline = deadline

    def _set_runtime_error(self, error: str | None) -> None:
        with self.state_lock:
            self.last_runtime_error = error

    def _wait_for_cycle_thread(self, timeout: float = 1.5) -> bool:
        with self.state_lock:
            cycle_thread = self.cycle_thread
        if not cycle_thread or cycle_thread is threading.current_thread():
            return True
        if cycle_thread.is_alive():
            cycle_thread.join(timeout=timeout)
        return not cycle_thread.is_alive()

    def _clear_cycle_pause(self, deadline: float | None = None) -> None:
        with self.state_lock:
            if deadline is not None and self.cycle_pause_deadline != deadline:
                return
            self.cycle_pause_phase = None
            self.cycle_pause_deadline = None

    def execute_movement(self) -> None:
        if self.is_switch_override_active():
            return

        with self.state_lock:
            target = self.target_pct
            duty = self.speed_duty
            self.last_cycle_end_reason = None
            self.last_runtime_error = None

        with self.hardware.lock:
            current = self.hardware.position_pct

        delta = target - current
        if abs(delta) < 0.5:
            with self.state_lock:
                self.active_target = None
            return

        self.hardware.resume()
        token = self._begin_target_movement(target)
        threading.Thread(target=self._move_worker, args=(delta, duty, token), daemon=True).start()

    def restart_movement(self, new_target: float) -> None:
        if self.is_switch_override_active():
            with self.state_lock:
                self.active_target = None
            return

        self.hardware.stop_pwm()
        time.sleep(0.1)

        with self.hardware.lock:
            current = self.hardware.position_pct
        delta = new_target - current
        if abs(delta) < 0.5:
            with self.state_lock:
                self.active_target = None
            return

        with self.state_lock:
            duty = self.speed_duty
            self.last_cycle_end_reason = None
            self.last_runtime_error = None

        self.hardware.resume()
        token = self._begin_target_movement(new_target)
        threading.Thread(target=self._move_worker, args=(delta, duty, token), daemon=True).start()

    def play_pause(self) -> None:
        with self.command_lock:
            if self.is_switch_override_active():
                with self.state_lock:
                    self.active_target = None
                return

            if self.is_counting_down():
                self.cancel_countdown()
                return

            with self.state_lock:
                is_cycling = self.is_cycling
                is_cycle_paused = self.is_cycle_paused
                delay = self.delay_sec

            if is_cycling:
                if is_cycle_paused:
                    with self.state_lock:
                        self.is_cycle_paused = False
                        self.last_runtime_error = None
                    self.hardware.resume()
                else:
                    with self.state_lock:
                        self.is_cycle_paused = True
                        self.last_runtime_error = None
                    with self.hardware.lock:
                        if self.hardware.is_moving:
                            self.hardware.pause()
                return

            with self.hardware.lock:
                is_moving = self.hardware.is_moving
                is_paused = self.hardware.is_paused

            if is_moving and not is_paused:
                with self.state_lock:
                    self.last_runtime_error = None
                self.hardware.pause()
                return

            if delay > 0 and not is_paused:
                self.start_countdown(delay)
                return

            self.execute_movement()

    def stop_cycle(self, reason: str = "stopped") -> None:
        with self.state_lock:
            self.cycle_token += 1
            self.is_cycling = False
            self.is_cycle_paused = False
            self.active_target = None
            self.cycle_pause_phase = None
            self.cycle_pause_deadline = None
            self.last_cycle_end_reason = reason
        self.hardware.stop_pwm()

    def toggle_cycle(self) -> None:
        with self.command_lock:
            if self.is_cycling:
                self.stop_cycle("stopped")
                return
            if self.is_switch_override_active():
                return
            with self.hardware.lock:
                if self.hardware.is_moving:
                    return

            if not self._wait_for_cycle_thread():
                with self.state_lock:
                    self.last_cycle_end_reason = "previous cycle still stopping"
                return
            self.cancel_countdown()
            self._clear_cycle_pause()
            with self.state_lock:
                self.cycle_token += 1
                cycle_token = self.cycle_token
                self.is_cycling = True
                self.is_cycle_paused = False
                self.last_cycle_end_reason = None
                self.last_runtime_error = None
                cycle_thread = threading.Thread(target=self._cycle_loop, args=(cycle_token,), daemon=True)
                self.cycle_thread = cycle_thread
            cycle_thread.start()

    def _cycle_wait(self, seconds: float, phase: str, cycle_token: int) -> bool:
        if seconds <= 0:
            self._clear_cycle_pause()
            return True

        remaining = seconds
        last_tick = time.time()
        self._set_cycle_pause(phase, last_tick + remaining)
        try:
            while True:
                with self.state_lock:
                    if cycle_token != self.cycle_token or not self.is_cycling:
                        return False
                    is_cycle_paused = self.is_cycle_paused
                if self.shutdown_event.is_set():
                    return False
                now = time.time()
                if is_cycle_paused:
                    last_tick = now
                    self._set_cycle_pause(phase, now + remaining)
                    time.sleep(0.1)
                    continue
                remaining -= max(0.0, now - last_tick)
                last_tick = now
                self._set_cycle_pause(phase, now + max(remaining, 0.0))
                if remaining <= 0:
                    return True
                time.sleep(min(0.1, remaining))
        finally:
            self._clear_cycle_pause()

    def _cycle_move_to_target(self, target_pct: float, duty: float, cycle_token: int) -> bool:
        while not self.shutdown_event.is_set():
            with self.state_lock:
                if cycle_token != self.cycle_token or not self.is_cycling:
                    return False
                is_cycle_paused = self.is_cycle_paused

            if is_cycle_paused:
                time.sleep(0.1)
                continue

            with self.hardware.lock:
                current = self.hardware.position_pct
            delta = target_pct - current
            if abs(delta) < 0.5:
                return True

            self.hardware.resume()
            token = self._begin_target_movement(target_pct)
            try:
                self.hardware.move_diff(delta, duty)
            finally:
                self._clear_target_movement(token)

            with self.state_lock:
                if cycle_token != self.cycle_token or not self.is_cycling:
                    return False
                paused_after_move = self.is_cycle_paused

            if paused_after_move:
                continue

            time.sleep(0.05)

        return False

    def _advance_sequence(self, step: int = 1, randomize: bool = False, persist: bool = True) -> None:
        ordered = self.library.ordered_ids()
        if not ordered:
            with self.state_lock:
                self.selected_sequence_id = None
            return

        with self.state_lock:
            current = self.selected_sequence_id
            if current not in ordered:
                self.selected_sequence_id = ordered[0]
            elif randomize and len(ordered) > 1:
                choices = [sequence_id for sequence_id in ordered if sequence_id != current]
                self.selected_sequence_id = random.choice(choices)
            else:
                index = ordered.index(current)
                self.selected_sequence_id = ordered[(index + step) % len(ordered)]

        if persist:
            self._save_settings()

    def previous_sequence(self) -> None:
        self._advance_sequence(step=-1)

    def next_sequence(self) -> None:
        self._advance_sequence(step=1)

    def _cycle_loop(self, cycle_token: int) -> None:
        cycle_end_reason: str | None = None
        try:
            while not self.shutdown_event.is_set():
                with self.state_lock:
                    if cycle_token != self.cycle_token or not self.is_cycling:
                        break
                    duty = self.speed_duty
                    pause_extend = self.cycle_pause_extend_sec
                    pause_retract = self.cycle_pause_retract_sec
                    randomize = self.cycle_random

                if not self._cycle_move_to_target(100.0, duty, cycle_token):
                    break
                with self.state_lock:
                    if cycle_token != self.cycle_token or not self.is_cycling:
                        break
                if not self._cycle_wait(pause_extend, "top", cycle_token):
                    break

                self._advance_sequence(randomize=randomize, persist=False)

                with self.state_lock:
                    if cycle_token != self.cycle_token or not self.is_cycling:
                        break
                if not self._cycle_move_to_target(0.0, duty, cycle_token):
                    break
                with self.state_lock:
                    if cycle_token != self.cycle_token or not self.is_cycling:
                        break
                if not self._cycle_wait(pause_retract, "bottom", cycle_token):
                    break

                self._advance_sequence(randomize=randomize, persist=False)
        except Exception as exc:
            cycle_end_reason = f"error: {exc}"
            print(f"Cycle loop stopped due to error: {exc}")
        finally:
            with self.state_lock:
                if self.cycle_thread is threading.current_thread():
                    self.cycle_thread = None
                is_current_cycle = cycle_token == self.cycle_token
                if is_current_cycle:
                    if cycle_end_reason is not None:
                        self.last_cycle_end_reason = cycle_end_reason
                    self.is_cycling = False
                    self.is_cycle_paused = False
                    self.active_target = None
                    self.cycle_pause_phase = None
                    self.cycle_pause_deadline = None
            if is_current_cycle:
                self.hardware.stop_pwm()
                self._save_settings()

    def start_jog(self, direction: int) -> None:
        if direction == 0 or self.is_switch_override_active():
            return
        with self.hardware.lock:
            if self.hardware.is_moving:
                return
        self.cancel_countdown()
        with self.state_lock:
            self.active_target = None
            self.cycle_pause_phase = None
            self.cycle_pause_deadline = None
            self.last_cycle_end_reason = None
            self.last_runtime_error = None
        self.hardware.resume()
        threading.Thread(target=self._manual_move_worker, args=(direction, self.speed_duty, "manual"), daemon=True).start()

    def stop_jog(self) -> None:
        self.hardware.stop_pwm()

    def home(self) -> None:
        if self.is_switch_override_active():
            return
        with self.hardware.lock:
            if self.hardware.is_moving:
                return
        self.cancel_countdown()
        with self.state_lock:
            self.active_target = None
            self.cycle_pause_phase = None
            self.cycle_pause_deadline = None
            self.last_cycle_end_reason = None
            self.last_runtime_error = None
        threading.Thread(target=self._home_worker, daemon=True).start()

    def stop_all(self) -> None:
        self.stop_cycle("stopped")
        self.cancel_countdown()
        with self.state_lock:
            self.active_target = None
            self.movement_token += 1
            self.is_cycle_paused = False
            self.cycle_pause_phase = None
            self.cycle_pause_deadline = None
            self.last_runtime_error = None
        with self.hardware.lock:
            self.hardware.is_paused = False
        self.hardware.stop_pwm()
        self.hardware.enable(False)

    def read_toggle_switch_direction(self) -> int:
        req = self.hardware.req
        if not req or gpiod is None:
            return 0
        try:
            extend_value = self.hardware._get_line_value(SW_EXTEND_IN)
            retract_value = self.hardware._get_line_value(SW_RETRACT_IN)
        except Exception:
            return 0

        extend_active = extend_value == gpiod.line.Value.ACTIVE
        retract_active = retract_value == gpiod.line.Value.ACTIVE

        if SW_ACTIVE_LOW:
            extend_pressed = not extend_active
            retract_pressed = not retract_active
        else:
            extend_pressed = extend_active
            retract_pressed = retract_active

        if extend_pressed and not retract_pressed:
            return 1
        if retract_pressed and not extend_pressed:
            return -1
        return 0

    def apply_switch_direction(self, direction: int) -> None:
        if direction == 0:
            self.hardware.stop_pwm()
            return
        self.cancel_countdown()
        self.stop_cycle("switch override")
        with self.state_lock:
            self.active_target = None
            self.last_runtime_error = None
        self.hardware.stop_pwm()
        self.hardware.resume()
        threading.Thread(target=self._manual_move_worker, args=(direction, self.speed_duty, "switch"), daemon=True).start()

    def is_switch_override_active(self) -> bool:
        with self.state_lock:
            return self.switch_stable_dir != 0

    def _poll_toggle_switch_loop(self) -> None:
        interval = SW_POLL_MS / 1000.0
        while not self.shutdown_event.is_set():
            direction_now = self.read_toggle_switch_direction()

            with self.state_lock:
                if direction_now == self.switch_raw_dir:
                    self.switch_same_count += 1
                else:
                    self.switch_raw_dir = direction_now
                    self.switch_same_count = 1

                should_apply = (
                    self.switch_same_count >= SW_DEBOUNCE_SAMPLES
                    and direction_now != self.switch_stable_dir
                )
                if should_apply:
                    self.switch_stable_dir = direction_now

            if should_apply:
                self.apply_switch_direction(direction_now)

            time.sleep(interval)

    def shutdown(self) -> None:
        self.shutdown_event.set()
        self.stop_all()
        self._cancel_scheduled_settings_save()
        self._save_settings()


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32


class ActuatorRequestHandler(BaseHTTPRequestHandler):
    server_version = "TimeVolumeActuator/1.0"
    protocol_version = "HTTP/1.1"
    controller: InstallationController | None = None
    pi_camera: Any = None

    def do_HEAD(self) -> None:
        self._handle_request(head_only=True)

    def do_GET(self) -> None:
        self._handle_request(head_only=False)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/settings":
            self._handle_settings_post()
            return
        if parsed.path == "/api/action":
            self._handle_action_post()
            return
        if parsed.path == "/api/pi-camera/record":
            self._handle_pi_camera_record_post()
            return
        self._json_error(HTTPStatus.NOT_FOUND, "Not found")

    def _handle_request(self, head_only: bool) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._redirect("/controller")
            return
        if path in {"/controller", "/controller/"}:
            self._serve_file(WEB_DIR / "controller.html", cache_control="no-store", head_only=head_only)
            return
        if path in {"/display", "/display/"}:
            self._serve_file(WEB_DIR / "display.html", cache_control="no-store", head_only=head_only)
            return
        if path == "/manifest.json":
            self._serve_file(WEB_DIR / "manifest.json", cache_control="no-cache", head_only=head_only)
            return
        if path == "/sw.js":
            self._serve_file(WEB_DIR / "sw.js", cache_control="no-cache", head_only=head_only)
            return
        if path.startswith("/icons/"):
            relative_path = unquote(path[len("/icons/"):])
            self._serve_file(BASE_DIR / "icons" / relative_path, cache_control="public, max-age=86400", head_only=head_only)
            return
        if path == "/api/state":
            self._send_json(self._controller_state(), head_only=head_only)
            return
        if path == "/api/pi-camera/state":
            self._send_json({"pi_camera": self._pi_camera_state()}, head_only=head_only)
            return
        if path == "/pi-camera/preview.jpg":
            self._serve_pi_camera_preview(parsed, head_only=head_only)
            return
        if path.startswith("/pi-camera/captures/"):
            self._serve_pi_camera_capture(path, head_only=head_only)
            return
        if path == "/api/library":
            controller = self._require_controller()
            self._send_json({"items": controller.get_library()}, head_only=head_only)
            return
        if path == "/api/sequence":
            controller = self._require_controller()
            params = parse_qs(parsed.query)
            sequence_id = params.get("id", [None])[0]
            detail = controller.get_sequence_detail(sequence_id)
            if not detail:
                self._json_error(HTTPStatus.NOT_FOUND, "Sequence not found")
                return
            self._send_json(detail, head_only=head_only)
            return
        if path == "/camera":
            camera_url = configured_camera_url()
            if camera_url and camera_url != "/camera/":
                self._redirect(camera_url)
                return
            if camera_app_available():
                self._redirect("/camera/")
                return
            self._json_error(HTTPStatus.NOT_FOUND, "Camera app is not installed")
            return
        if path.startswith("/camera/"):
            camera_url = configured_camera_url()
            if camera_url and camera_url != "/camera/":
                self._redirect(camera_url)
                return
            self._serve_camera_asset(path, head_only=head_only)
            return
        if path.startswith("/media/"):
            self._serve_media_asset(path, head_only=head_only)
            return
        self._json_error(HTTPStatus.NOT_FOUND, "Not found")

    def _handle_settings_post(self) -> None:
        controller = self._require_controller()
        try:
            payload = self._read_json_body()
            state = controller.set_settings(payload)
        except ValueError as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except Exception as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, f"Invalid settings payload: {exc}")
            return
        self._send_json({"state": self._with_pi_camera_state(state)})

    def _handle_action_post(self) -> None:
        controller = self._require_controller()
        try:
            payload = self._read_json_body()
        except Exception as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, f"Invalid action payload: {exc}")
            return

        action = payload.get("action")
        try:
            if action == "play_pause":
                controller.play_pause()
            elif action == "stop":
                controller.stop_all()
            elif action == "home":
                controller.home()
            elif action == "cycle_toggle":
                controller.toggle_cycle()
            elif action == "jog_start":
                controller.start_jog(int(payload.get("direction", 0)))
            elif action == "jog_stop":
                controller.stop_jog()
            elif action == "refresh_library":
                controller.refresh_library()
            elif action == "next_sequence":
                controller.next_sequence()
            elif action == "previous_sequence":
                controller.previous_sequence()
            else:
                self._json_error(HTTPStatus.BAD_REQUEST, "Unknown action")
                return
        except Exception as exc:
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Action failed: {exc}")
            return

        self._send_json({"state": self._controller_state()})

    def _handle_pi_camera_record_post(self) -> None:
        runtime = self.pi_camera
        if runtime is None or RecordSettings is None:
            self._json_error(HTTPStatus.NOT_FOUND, PI_CAMERA_IMPORT_ERROR or "Pi camera experiment is not installed")
            return

        try:
            payload = self._read_json_body()
            settings = RecordSettings.from_payload(payload)
            state = runtime.start_recording(settings, self._run_camera_action)
        except ValueError as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except CameraUnavailableError as exc:
            self._json_error(HTTPStatus.NOT_FOUND, str(exc))
            return
        except CameraBusyError as exc:
            self._json_error(HTTPStatus.CONFLICT, str(exc))
            return
        except Exception as exc:
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Pi camera record failed: {exc}")
            return

        self._send_json({"pi_camera": state})

    def _run_camera_action(self, action: str) -> None:
        controller = self._require_controller()
        if action == "play_pause":
            controller.play_pause()
            return
        if action == "stop":
            controller.stop_all()
            return
        raise ValueError(f"Unsupported Pi camera action: {action}")

    def _controller_state(self) -> dict[str, Any]:
        controller = self._require_controller()
        return self._with_pi_camera_state(controller.get_state())

    def _with_pi_camera_state(self, state: dict[str, Any]) -> dict[str, Any]:
        state["pi_camera"] = self._pi_camera_state()
        return state

    def _pi_camera_state(self) -> dict[str, Any]:
        runtime = self.pi_camera
        if runtime is None:
            return {
                "available": False,
                "label": None,
                "reason": PI_CAMERA_IMPORT_ERROR or "Pi camera experiment is not installed",
                "busy": False,
                "job": None,
                "last_capture_url": None,
                "last_error": None,
            }
        return runtime.get_state()

    def _require_controller(self) -> InstallationController:
        if self.controller is None:
            raise RuntimeError("Controller not initialized")
        return self.controller

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK, head_only: bool = False) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_pi_camera_preview(self, parsed: Any, head_only: bool) -> None:
        runtime = self.pi_camera
        if runtime is None:
            self._json_error(HTTPStatus.NOT_FOUND, PI_CAMERA_IMPORT_ERROR or "Pi camera experiment is not installed")
            return

        try:
            params = parse_qs(parsed.query)
            preview_manual = str(params.get("manual_exposure", ["false"])[0]).lower() in {"1", "true", "yes", "on", "manual"}
            payload: dict[str, Any] = {
                "capture_mode": "640x480",
                "manual_exposure": preview_manual,
                "frame_gap_sec": 0,
            }
            if preview_manual:
                payload["shutter_sec"] = params.get("shutter_sec", [None])[0]
                payload["iso"] = params.get("iso", [None])[0]
            settings = RecordSettings.from_payload(payload) if RecordSettings is not None else None
            body = b"" if head_only else runtime.capture_preview_jpeg(settings=settings)
        except ValueError as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except CameraUnavailableError as exc:
            self._json_error(HTTPStatus.NOT_FOUND, str(exc))
            return
        except CameraBusyError as exc:
            self._json_error(HTTPStatus.CONFLICT, str(exc))
            return
        except Exception as exc:
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Pi camera preview failed: {exc}")
            return

        if head_only:
            body = b""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _serve_pi_camera_capture(self, request_path: str, head_only: bool) -> None:
        runtime = self.pi_camera
        if runtime is None:
            self._json_error(HTTPStatus.NOT_FOUND, PI_CAMERA_IMPORT_ERROR or "Pi camera experiment is not installed")
            return
        file_path = runtime.capture_path_for_request(request_path)
        if not file_path:
            self._json_error(HTTPStatus.NOT_FOUND, "Pi camera capture not found")
            return
        self._serve_file(file_path, cache_control="no-store", head_only=head_only)

    def _serve_camera_asset(self, request_path: str, head_only: bool) -> None:
        relative = unquote(request_path[len("/camera/") :]).replace("\\", "/")
        if relative in {"", "index.html"}:
            relative = "index.html"
        file_path = self._safe_join(CAMERA_APP_DIR, relative)
        if not file_path or not file_path.is_file():
            self._json_error(HTTPStatus.NOT_FOUND, "Camera asset not found")
            return
        self._serve_file(file_path, cache_control="no-store", head_only=head_only)

    def _serve_media_asset(self, request_path: str, head_only: bool) -> None:
        relative = unquote(request_path[len("/media/") :]).replace("\\", "/")
        safe_path = self._safe_join(MEDIA_ROOT, relative)
        if not safe_path or not safe_path.is_file():
            self._json_error(HTTPStatus.NOT_FOUND, "Media asset not found")
            return
        self._serve_file(safe_path, cache_control="public, max-age=3600", head_only=head_only)

    def _safe_join(self, root: Path, relative_path: str) -> Path | None:
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return None
        return candidate

    def _serve_file(self, file_path: Path, cache_control: str = "no-store", head_only: bool = False) -> None:
        if not file_path.exists() or not file_path.is_file():
            self._json_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        file_size = file_path.stat().st_size
        range_header = self.headers.get("Range")
        start = 0
        end = file_size - 1
        status = HTTPStatus.OK

        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            start_str, end_str = match.groups()
            if start_str:
                start = int(start_str)
                end = int(end_str) if end_str else file_size - 1
            else:
                suffix_length = int(end_str)
                start = max(0, file_size - suffix_length)
                end = file_size - 1

            if start > end or end >= file_size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            status = HTTPStatus.PARTIAL_CONTENT

        content_length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(content_length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        if head_only:
            return

        with file_path.open("rb") as handle:
            handle.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, fmt: str, *args: Any) -> None:
        path = urlparse(self.path).path
        status = None
        if len(args) > 1:
            try:
                status = int(args[1])
            except (TypeError, ValueError):
                status = None

        if status is not None and 200 <= status < 400:
            if path == "/api/state" or path.startswith("/media/"):
                return

        message = "%s - - [%s] %s\n" % (
            self.address_string(),
            self.log_date_time_string(),
            fmt % args,
        )
        print(message, end="")


class MpvDisplayBackend:
    def __init__(
        self,
        controller: InstallationController,
        mpv_bin: str = "mpv",
        fps_cap: float = MPV_DEFAULT_FPS_CAP,
    ):
        self.controller = controller
        self.mpv_bin = mpv_bin
        self.fps_cap = max(1.0, float(fps_cap))
        self.process: subprocess.Popen[Any] | None = None
        self.sock: socket.socket | None = None
        self.reader: Any = None
        self.socket_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.request_id = 0
        self.loaded_key: tuple[Any, ...] | None = None
        self.last_pause: bool | None = None
        self.last_speed: float | None = None
        self.last_seek_at = 0.0
        self.duration_cache: dict[tuple[Any, ...], float] = {}
        self.playlist_dir = RUN_DIR / "mpv_playlists"
        self.last_direction = 1

    def start(self) -> bool:
        mpv_path = shutil.which(self.mpv_bin)
        if not mpv_path:
            self.controller.set_display_backend_status("browser", f"MPV not found; use /display")
            print("Warning: MPV display backend requested, but mpv was not found. Use /display fallback.")
            return False

        RUN_DIR.mkdir(parents=True, exist_ok=True)
        self.playlist_dir.mkdir(parents=True, exist_ok=True)
        self._write_black_image()

        try:
            if MPV_IPC_PATH.exists():
                MPV_IPC_PATH.unlink()
        except Exception:
            pass

        command = [
            mpv_path,
            "--idle=yes",
            "--force-window=immediate",
            "--fullscreen",
            "--no-terminal",
            "--really-quiet",
            "--osc=no",
            "--osd-level=0",
            "--keep-open=yes",
            "--loop-file=no",
            f"--input-ipc-server={MPV_IPC_PATH}",
        ]

        try:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL)
            MPV_PID_FILE.write_text(str(self.process.pid), encoding="utf-8")
            self._connect()
        except Exception as exc:
            self.controller.set_display_backend_status("browser", f"MPV failed: {exc}")
            print(f"Warning: MPV display backend failed to start ({exc}). Use /display fallback.")
            self.stop()
            return False

        self.controller.set_display_backend_status("mpv", "MPV fullscreen display")
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=1.0)
        with self.socket_lock:
            try:
                if self.sock:
                    self._send_command_unlocked(["quit"])
            except Exception:
                pass
            try:
                if self.reader:
                    self.reader.close()
            except Exception:
                pass
            try:
                if self.sock:
                    self.sock.close()
            except Exception:
                pass
            self.reader = None
            self.sock = None

        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=1.5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.process = None
        try:
            MPV_PID_FILE.unlink()
        except Exception:
            pass
        try:
            MPV_IPC_PATH.unlink()
        except Exception:
            pass

    def _write_black_image(self) -> None:
        if MPV_BLACK_IMAGE.exists():
            return
        # 1x1 black PNG.
        MPV_BLACK_IMAGE.write_bytes(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
                "0000000c4944415408d7636060000000050001a5f645400000000049454e44ae426082"
            )
        )

    def _connect(self) -> None:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self.process and self.process.poll() is not None:
                raise RuntimeError("mpv exited before IPC became available")
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(str(MPV_IPC_PATH))
                sock.settimeout(0.25)
                self.sock = sock
                self.reader = sock.makefile("r", encoding="utf-8")
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("timed out waiting for mpv IPC")

    def _send_command_unlocked(self, command: list[Any], timeout: float = 0.25) -> dict[str, Any] | None:
        if not self.sock:
            self._connect()
        self.request_id += 1
        request_id = self.request_id
        payload = json.dumps({"command": command, "request_id": request_id}).encode("utf-8") + b"\n"
        assert self.sock is not None
        self.sock.sendall(payload)

        if not self.reader:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self.reader.readline()
            except socket.timeout:
                return None
            if not line:
                return None
            try:
                response = json.loads(line)
            except Exception:
                continue
            if response.get("request_id") == request_id:
                return response
        return None

    def _command(self, command: list[Any], timeout: float = 0.25) -> dict[str, Any] | None:
        with self.socket_lock:
            try:
                return self._send_command_unlocked(command, timeout=timeout)
            except Exception:
                try:
                    if self.reader:
                        self.reader.close()
                    if self.sock:
                        self.sock.close()
                except Exception:
                    pass
                self.reader = None
                self.sock = None
                try:
                    self._connect()
                    return self._send_command_unlocked(command, timeout=timeout)
                except Exception:
                    return None

    def _set_pause(self, paused: bool) -> None:
        if self.last_pause == paused:
            return
        self._command(["set_property", "pause", paused])
        self.last_pause = paused

    def _set_speed(self, speed: float) -> None:
        speed = clamp(speed, 0.01, 100.0)
        if self.last_speed is not None and abs(self.last_speed - speed) < 0.01:
            return
        self._command(["set_property", "speed", speed])
        self.last_speed = speed

    def _seek(self, seconds: float, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_seek_at < 0.18:
            return
        self._command(["set_property", "time-pos", max(0.0, seconds)])
        self.last_seek_at = now

    def _load_black(self) -> None:
        key = ("black",)
        if self.loaded_key != key:
            self._command(["loadfile", str(MPV_BLACK_IMAGE), "replace"])
            self.loaded_key = key
            self.last_pause = None
            self.last_speed = None
        self._set_speed(1.0)
        self._set_pause(True)

    def _direction_for_state(self, state: dict[str, Any]) -> int:
        switch_direction = int(state.get("switch_direction") or 0)
        if switch_direction:
            self.last_direction = 1 if switch_direction > 0 else -1
            return self.last_direction

        position_pct = float(state.get("position_pct") or 0.0)
        for key in ("active_target_pct", "target_pct"):
            value = state.get(key)
            if isinstance(value, (int, float)) and abs(float(value) - position_pct) > 0.05:
                self.last_direction = 1 if float(value) > position_pct else -1
                return self.last_direction
        return self.last_direction

    def _image_plan(self, sequence: SequenceItem, state: dict[str, Any], direction: int) -> dict[str, Any]:
        duty = float(state.get("speed_duty") or 1.0)
        stride, requested_fps, duration_sec = frame_stride_for_sequence(sequence, direction, duty, self.fps_cap)
        last_index = max(0, sequence.frame_count - 1)
        indexes = list(range(0, last_index + 1, stride))
        if indexes[-1] != last_index:
            indexes.append(last_index)
        if direction < 0:
            indexes.reverse()

        frame_count = max(1, len(indexes))
        fps = clamp((frame_count - 1) / max(0.01, duration_sec), 0.1, self.fps_cap)
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", sequence.id)[:80] or "sequence"
        playlist_path = self.playlist_dir / f"{safe_id}_{'up' if direction >= 0 else 'down'}_s{stride}.txt"

        if not playlist_path.exists():
            playlist_path.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            for index in indexes:
                relative_path = sequence.frame_paths[index]
                lines.append(str((MEDIA_ROOT / relative_path).resolve()))
            playlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        return {
            "key": ("images", sequence.id, direction, stride, sequence.frame_count, round(fps, 3)),
            "source": f"mf://@{playlist_path}",
            "options": {"mf-fps": f"{fps:.6f}"},
            "duration_sec": duration_sec,
            "fps": fps,
            "stride": stride,
            "requested_fps": requested_fps,
        }

    def _load_source(self, key: tuple[Any, ...], source: str, options: dict[str, Any] | None = None) -> bool:
        if self.loaded_key == key:
            return True
        if options and "mf-fps" in options:
            try:
                self._command(["set_property", "mf-fps", float(options["mf-fps"])])
            except Exception:
                pass
        response = self._command(["loadfile", source, "replace"], timeout=0.5)
        if not response or response.get("error") != "success":
            error = response.get("error") if response else "no response"
            self.loaded_key = None
            self.controller.set_display_backend_status("mpv", f"MPV load failed: {error}")
            return False
        self.loaded_key = key
        self.last_pause = None
        self.last_speed = None
        self.last_seek_at = 0.0
        return True

    def _duration_for_loaded_video(self, key: tuple[Any, ...]) -> float | None:
        cached = self.duration_cache.get(key)
        if cached:
            return cached
        response = self._command(["get_property", "duration"], timeout=0.4)
        duration = response.get("data") if response else None
        if isinstance(duration, (int, float)) and duration > 0:
            self.duration_cache[key] = float(duration)
            return float(duration)
        return None

    def _sync_images(self, sequence: SequenceItem, state: dict[str, Any], ratio: float, direction: int, moving: bool) -> None:
        plan = self._image_plan(sequence, state, direction)
        if not self._load_source(plan["key"], plan["source"], plan["options"]):
            self._load_black()
            return
        playback_ratio = ratio if direction >= 0 else 1.0 - ratio
        self._set_speed(1.0)
        if not moving:
            self._set_pause(True)
            self._seek(playback_ratio * plan["duration_sec"], force=True)
            return
        if self.last_pause is not False:
            self._seek(playback_ratio * plan["duration_sec"], force=True)
        self._set_pause(False)

    def _sync_video(self, sequence: SequenceItem, state: dict[str, Any], ratio: float, direction: int, moving: bool) -> None:
        source_path = (MEDIA_ROOT / sequence.relative_path).resolve()
        key = ("video", sequence.id, str(source_path))
        if not self._load_source(key, str(source_path)):
            self._load_black()
            return
        duration = self._duration_for_loaded_video(key)
        if not duration:
            self._set_pause(True)
            return

        duty = float(state.get("speed_duty") or 1.0)
        travel_duration = max(0.01, sequence_travel_duration_sec(sequence, direction, duty))
        target_time = clamp(ratio, 0.0, 1.0) * duration

        if not moving or direction < 0:
            self._set_pause(True)
            self._seek(target_time, force=not moving)
            return

        self._set_speed(duration / travel_duration)
        if self.last_pause is not False:
            self._seek(target_time, force=True)
        self._set_pause(False)

    def _sync_state(self) -> None:
        state = self.controller.get_state()
        sequence = self.controller.library.get(state.get("selected_sequence_id"))
        if not sequence:
            self._load_black()
            return

        position_pct = float(state.get("position_pct") or 0.0)
        ratio = sequence.playback_ratio_for_pct(position_pct)
        if ratio is None:
            self._load_black()
            return

        direction = self._direction_for_state(state)
        moving = bool(state.get("is_moving")) and not bool(state.get("is_paused"))
        if sequence.kind == "images":
            self._sync_images(sequence, state, ratio, direction, moving)
        elif sequence.kind == "video":
            self._sync_video(sequence, state, ratio, direction, moving)
        else:
            self._load_black()

    def _run_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                if self.process and self.process.poll() is not None:
                    self.controller.set_display_backend_status("browser", "MPV exited; use /display")
                    try:
                        MPV_PID_FILE.unlink()
                    except Exception:
                        pass
                    return
                self._sync_state()
            except Exception as exc:
                self.controller.set_display_backend_status("mpv", f"MPV sync error: {exc}")
            sleep_time = 0.05
            try:
                with self.controller.hardware.lock:
                    moving = self.controller.hardware.is_moving and not self.controller.hardware.is_paused
                sleep_time = 0.05 if moving else 0.2
            except Exception:
                pass
            self.stop_event.wait(sleep_time)


def request_gpio_lines(require_gpio: bool = False) -> Any:
    output_offsets = (RPWM, LPWM, REN, LEN)
    input_offsets = (SW_EXTEND_IN, SW_RETRACT_IN)

    if gpiod is None:
        message = "gpiod is unavailable"
        if require_gpio:
            raise RuntimeError(message)
        print(f"Warning: {message}. Running in simulation mode.")
        return None

    try:
        return gpiod.request_lines(
            CHIPPATH,
            consumer="time-volume-actuator",
            config={
                output_offsets: gpiod.LineSettings(
                    direction=gpiod.line.Direction.OUTPUT,
                    output_value=gpiod.line.Value.INACTIVE,
                ),
                input_offsets: gpiod.LineSettings(
                    direction=gpiod.line.Direction.INPUT,
                    bias=gpiod.line.Bias.PULL_UP,
                ),
            },
        )
    except Exception as exc:
        message = f"Could not open GPIO ({exc})"
        if require_gpio:
            raise RuntimeError(message) from exc
        print(f"Warning: {message}. Running in simulation mode.")
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Host the Time Volume actuator controller, display page, and camera app."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"HTTP bind host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP bind port (default: {DEFAULT_PORT})")
    parser.add_argument("--require-gpio", action="store_true", help="Exit instead of running in simulation mode if GPIO is unavailable")
    parser.add_argument(
        "--display-backend",
        choices=("browser", "mpv", "none"),
        default=os.environ.get("TIME_VOLUME_DISPLAY_BACKEND", DEFAULT_DISPLAY_BACKEND),
        help=f"Fullscreen display backend to manage from the server (default: {DEFAULT_DISPLAY_BACKEND})",
    )
    parser.add_argument("--mpv-bin", default=os.environ.get("TIME_VOLUME_MPV_BIN", "mpv"), help="MPV executable for --display-backend mpv")
    parser.add_argument("--mpv-fps-cap", type=float, default=float(os.environ.get("TIME_VOLUME_MPV_FPS_CAP", MPV_DEFAULT_FPS_CAP)), help="Maximum sampled image-sequence FPS for MPV")
    return parser


def print_launch_hints(host: str, port: int) -> None:
    hostname = socket.gethostname()
    camera_url = configured_camera_url()
    print()
    print("Time Volume web server is running.")
    print(f"Controller: http://localhost:{port}/controller")
    print(f"Display   : http://localhost:{port}/display")
    if camera_url == "/camera/":
        print(f"Camera    : http://localhost:{port}/camera/")
    elif camera_url:
        print(f"Camera    : {camera_url}")
    else:
        print("Camera    : not configured")
    if host in {"0.0.0.0", "::"}:
        print(f"On your network, try: http://{hostname}.local:{port}/controller")
        if camera_url == "/camera/":
            print(f"Camera on your network: http://{hostname}.local:{port}/camera/")
    print(f"Media folder: {MEDIA_ROOT}")
    print("Copy image-sequence folders or video files into the media folder, then tap Refresh Library.")
    print()


def main() -> None:
    args = build_parser().parse_args()
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    WEB_DIR.mkdir(parents=True, exist_ok=True)

    req = request_gpio_lines(require_gpio=args.require_gpio)
    controller = InstallationController(req)
    ActuatorRequestHandler.controller = controller
    ActuatorRequestHandler.pi_camera = (
        PiCameraRuntime(BASE_DIR / "experiments" / "pi_long_exposure" / "captures")
        if PiCameraRuntime is not None
        else None
    )
    mpv_display: MpvDisplayBackend | None = None
    if args.display_backend == "mpv":
        mpv_display = MpvDisplayBackend(controller, mpv_bin=args.mpv_bin, fps_cap=args.mpv_fps_cap)
        if not mpv_display.start():
            mpv_display = None
    elif args.display_backend == "none":
        controller.set_display_backend_status("none", "No fullscreen display backend")

    server = ReusableThreadingHTTPServer((args.host, args.port), ActuatorRequestHandler)

    print_launch_hints(args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.shutdown()
        server.server_close()
        if mpv_display:
            mpv_display.stop()
        controller.shutdown()
        if req:
            try:
                req.release()
            except Exception:
                pass


if __name__ == "__main__":
    main()
