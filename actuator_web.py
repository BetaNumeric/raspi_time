#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import random
import re
import socket
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


CHIPPATH = "/dev/gpiochip0"
RPWM = 18
LPWM = 19
REN = 23
LEN = 24
PWM_HZ = 200
EXTEND_SEC = 23.75
RETRACT_SEC = 22.0
HOME_TIMEOUT_SEC = 25.0

SW_EXTEND_IN = 6
SW_RETRACT_IN = 5
SW_ACTIVE_LOW = True
SW_POLL_MS = 30
SW_DEBOUNCE_SAMPLES = 2

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
MEDIA_ROOT = BASE_DIR / "media"
STATE_FILE = BASE_DIR / "actuator_state.json"
DEFAULT_HOST = os.environ.get("ACTUATOR_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("ACTUATOR_PORT", "8000"))

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def media_url_for(relative_path: str) -> str:
    return f"/media/{quote(relative_path, safe='/')}"


@dataclass
class SequenceItem:
    id: str
    name: str
    kind: str
    relative_path: str
    frame_count: int = 0
    frame_paths: list[str] = field(default_factory=list)
    media_url: str | None = None

    def frame_index_for_pct(self, position_pct: float) -> int:
        if self.kind != "images" or self.frame_count <= 1:
            return 0
        ratio = clamp(position_pct, 0.0, 100.0) / 100.0
        return min(self.frame_count - 1, int(round(ratio * (self.frame_count - 1))))

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "relative_path": self.relative_path,
            "frame_count": self.frame_count,
            "media_url": self.media_url,
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
                    items[rel_path] = SequenceItem(
                        id=rel_path,
                        name=file_path.stem,
                        kind="video",
                        relative_path=rel_path,
                        media_url=media_url_for(rel_path),
                    )
                elif suffix in IMAGE_EXTENSIONS:
                    image_paths.append(rel_path)

            if not image_paths:
                continue

            sequence_id = rel_dir or "__root__"
            display_name = directory.name if rel_dir else "media"
            items[sequence_id] = SequenceItem(
                id=sequence_id,
                name=display_name,
                kind="images",
                relative_path=rel_dir or ".",
                frame_count=len(image_paths),
                frame_paths=image_paths,
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
            "frame_index": sequence.frame_index_for_pct(position_pct) if sequence else 0,
            "is_simulation": self.hardware.req is None,
            "media_root": str(MEDIA_ROOT),
            "controller_url": "/controller",
            "display_url": "/display",
            "camera_url": "/camera/",
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
        if path == "/api/state":
            controller = self._require_controller()
            self._send_json(controller.get_state(), head_only=head_only)
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
            self._redirect("/camera/")
            return
        if path.startswith("/camera/"):
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
        self._send_json({"state": state})

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

        self._send_json({"state": controller.get_state()})

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

    def _serve_camera_asset(self, request_path: str, head_only: bool) -> None:
        relative = request_path[len("/camera/") :]
        if relative in {"", "index.html"}:
            file_path = BASE_DIR / "index.html"
        elif relative in {"manifest.json", "service-worker.js"}:
            file_path = BASE_DIR / relative
        elif relative.startswith("icons/"):
            file_path = BASE_DIR / relative
        else:
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


def request_gpio_lines() -> Any:
    output_offsets = (RPWM, LPWM, REN, LEN)
    input_offsets = (SW_EXTEND_IN, SW_RETRACT_IN)

    if gpiod is None:
        print("Warning: gpiod is unavailable. Running in simulation mode.")
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
        print(f"Warning: Could not open GPIO. Running in simulation mode. ({exc})")
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Host the Time Volume actuator controller, display page, and camera app."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"HTTP bind host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP bind port (default: {DEFAULT_PORT})")
    return parser


def print_launch_hints(host: str, port: int) -> None:
    hostname = socket.gethostname()
    print()
    print("Time Volume web server is running.")
    print(f"Controller: http://localhost:{port}/controller")
    print(f"Display   : http://localhost:{port}/display")
    print(f"Camera    : http://localhost:{port}/camera/")
    if host in {"0.0.0.0", "::"}:
        print(f"On your network, try: http://{hostname}.local:{port}/controller")
    print(f"Media folder: {MEDIA_ROOT}")
    print("Copy image-sequence folders or video files into the media folder, then tap Refresh Library.")
    print()


def main() -> None:
    args = build_parser().parse_args()
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    WEB_DIR.mkdir(parents=True, exist_ok=True)

    req = request_gpio_lines()
    controller = InstallationController(req)
    ActuatorRequestHandler.controller = controller
    server = ReusableThreadingHTTPServer((args.host, args.port), ActuatorRequestHandler)

    print_launch_hints(args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.shutdown()
        server.server_close()
        controller.shutdown()
        if req:
            try:
                req.release()
            except Exception:
                pass


if __name__ == "__main__":
    main()
