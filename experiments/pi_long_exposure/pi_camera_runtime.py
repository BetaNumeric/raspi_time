from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote

from .pi_long_exposure import (
    blend_frame,
    build_camera_controls,
    finalize_image,
    load_camera_stack,
    prepare_frame,
    save_image,
)


ActionCallback = Callable[[str], None]

BLEND_MODES = {"lighten", "mean", "screen", "add"}
OV5647_CAPTURE_MODES = [
    {"key": "640x480", "label": "640 x 480", "width": 640, "height": 480, "max_fps": 58.92},
    {"key": "1296x972", "label": "1296 x 972", "width": 1296, "height": 972, "max_fps": 46.34},
    {"key": "1920x1080", "label": "1920 x 1080", "width": 1920, "height": 1080, "max_fps": 32.81},
    {"key": "2592x1944", "label": "2592 x 1944", "width": 2592, "height": 1944, "max_fps": 15.63},
]
DEFAULT_CAPTURE_MODE_KEY = "1296x972"
MAX_FRAME_INTERVAL_SEC = 4.879
OV5647_LIMITS = {
    "duration_sec": {"min": 1, "max": 30, "step": 1, "default": 25},
    "frame_gap_sec": {"min": 0, "max": 4, "step": 0.01, "default": 0},
    "shutter_sec": {"min": 0.01, "max": 4.8, "step": 0.01, "default": 1.0},
    "iso": {"min": 100, "max": 6350, "step": 50, "default": 100},
    "threshold": {"min": 0, "max": 255, "step": 1, "default": 10},
}


class CameraUnavailableError(RuntimeError):
    pass


class CameraBusyError(RuntimeError):
    pass


def camera_limits_payload() -> dict[str, Any]:
    return {
        "capture_modes": OV5647_CAPTURE_MODES,
        "default_capture_mode": DEFAULT_CAPTURE_MODE_KEY,
        "blend_modes": [
            {"value": "add", "label": "Additive"},
            {"value": "lighten", "label": "Lighten"},
            {"value": "mean", "label": "Mean"},
            {"value": "screen", "label": "Screen"},
        ],
        **OV5647_LIMITS,
        "aperture": {"label": "Fixed"},
    }


def clamp_number(value: Any, fallback: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


@dataclass
class RecordSettings:
    duration: float = 20.0
    mode: str = "lighten"
    threshold: int = 0
    width: int = 1280
    height: int = 720
    fps: float = 12.0
    quality: int = 92
    warmup: float = 1.0
    frame_interval_sec: float = 1.0
    frame_gap_sec: float = 0.0
    manual_exposure: bool = True
    shutter_sec: float | None = None
    iso: int | None = None
    hflip: bool = False
    vflip: bool = False
    trigger_lift: bool = False
    stop_after: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RecordSettings:
        mode = str(payload.get("mode", "add"))
        if mode not in BLEND_MODES:
            raise ValueError("Unknown Pi camera blend mode")

        capture_mode = cls._capture_mode_from_payload(payload)
        min_interval = 1.0 / float(capture_mode["max_fps"])
        max_interval = MAX_FRAME_INTERVAL_SEC
        frame_gap_sec = clamp_number(
            payload.get("frame_gap_sec", payload.get("frame_interval_sec")),
            OV5647_LIMITS["frame_gap_sec"]["default"],
            OV5647_LIMITS["frame_gap_sec"]["min"],
            OV5647_LIMITS["frame_gap_sec"]["max"],
        )
        manual_exposure = bool(payload.get("manual_exposure", True))
        shutter_sec: float | None = None
        iso: int | None = None
        if manual_exposure:
            shutter_sec = clamp_number(
                payload.get("shutter_sec"),
                OV5647_LIMITS["shutter_sec"]["default"],
                OV5647_LIMITS["shutter_sec"]["min"],
                OV5647_LIMITS["shutter_sec"]["max"],
            )
            frame_interval_sec = max(min_interval, min(max_interval, shutter_sec + frame_gap_sec))
            iso = int(clamp_number(
                payload.get("iso"),
                OV5647_LIMITS["iso"]["default"],
                OV5647_LIMITS["iso"]["min"],
                OV5647_LIMITS["iso"]["max"],
            ))
        else:
            frame_interval_sec = max(min_interval, min(max_interval, frame_gap_sec))

        return cls(
            duration=clamp_number(
                payload.get("duration"),
                OV5647_LIMITS["duration_sec"]["default"],
                OV5647_LIMITS["duration_sec"]["min"],
                OV5647_LIMITS["duration_sec"]["max"],
            ),
            mode=mode,
            threshold=int(clamp_number(
                payload.get("threshold"),
                OV5647_LIMITS["threshold"]["default"],
                OV5647_LIMITS["threshold"]["min"],
                OV5647_LIMITS["threshold"]["max"],
            )),
            width=int(capture_mode["width"]),
            height=int(capture_mode["height"]),
            fps=1.0 / frame_interval_sec,
            quality=int(clamp_number(payload.get("quality"), 92, 1, 100)),
            warmup=clamp_number(payload.get("warmup"), 1.0, 0.0, 10.0),
            frame_interval_sec=frame_interval_sec,
            frame_gap_sec=frame_gap_sec,
            manual_exposure=manual_exposure,
            shutter_sec=shutter_sec,
            iso=iso,
            hflip=bool(payload.get("hflip", False)),
            vflip=bool(payload.get("vflip", False)),
            trigger_lift=bool(payload.get("trigger_lift", False)),
            stop_after=bool(payload.get("stop_after", False)),
        )

    @classmethod
    def _capture_mode_from_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        requested_key = str(payload.get("capture_mode") or "")
        if not requested_key and "width" in payload and "height" in payload:
            requested_key = f"{payload.get('width')}x{payload.get('height')}"
        if not requested_key:
            requested_key = DEFAULT_CAPTURE_MODE_KEY

        for mode in OV5647_CAPTURE_MODES:
            if requested_key == mode["key"]:
                return mode
        raise ValueError("Unsupported OV5647 capture mode")

    @property
    def shutter_us(self) -> int | None:
        if self.shutter_sec is None:
            return None
        return int(self.shutter_sec * 1_000_000)

    @property
    def analogue_gain(self) -> float | None:
        if self.iso is None:
            return None
        return max(1.0, self.iso / 100.0)


class PiCameraRuntime:
    def __init__(self, capture_dir: Path):
        self.capture_dir = capture_dir
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.camera_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.detect_cache_until = 0.0
        self.detect_available = False
        self.detect_label: str | None = None
        self.detect_error: str | None = None
        self.job: dict[str, Any] | None = None
        self.last_capture_path: Path | None = None
        self.last_frame_count = 0
        self.last_error: str | None = None

    def get_state(self) -> dict[str, Any]:
        available = self.is_available()
        now = time.time()
        with self.state_lock:
            job = dict(self.job) if self.job else None
            last_capture_path = self.last_capture_path
            last_frame_count = self.last_frame_count
            last_error = self.last_error
            label = self.detect_label
            reason = self.detect_error

        if job:
            elapsed = max(0.0, now - float(job["started_at"]))
            job["elapsed_sec"] = round(elapsed, 1)
            job["remaining_sec"] = round(max(0.0, float(job["duration"]) - elapsed), 1)

        return {
            "available": available,
            "label": label if available else None,
            "reason": None if available else reason,
            "busy": job is not None,
            "job": job,
            "last_capture_url": self._url_for_path(last_capture_path) if last_capture_path else None,
            "last_frame_count": last_frame_count,
            "last_error": last_error,
            "limits": camera_limits_payload(),
        }

    def is_available(self) -> bool:
        now = time.monotonic()
        with self.state_lock:
            if now < self.detect_cache_until:
                return self.detect_available
            if self.camera_lock.locked() and self.detect_cache_until > 0:
                return self.detect_available

        try:
            _, _, Picamera2 = load_camera_stack()
            cameras = Picamera2.global_camera_info()
            available = bool(cameras)
            label = self._format_camera_label(cameras[0]) if available else None
            error = None if available else "No Pi camera detected"
        except Exception as exc:
            available = False
            label = None
            error = str(exc)

        with self.state_lock:
            self.detect_available = available
            self.detect_label = label
            self.detect_error = error
            self.detect_cache_until = now + (30.0 if available else 10.0)
        return available

    def capture_preview_jpeg(self, settings: RecordSettings | None = None, quality: int = 82) -> bytes:
        self._require_available()
        if not self.camera_lock.acquire(timeout=2.0):
            raise CameraBusyError("Pi camera is busy")

        picam2 = None
        camera_started = False
        width = 640
        height = 480
        settings = settings or RecordSettings.from_payload({
            "capture_mode": "640x480",
            "manual_exposure": False,
            "frame_gap_sec": 0,
        })
        try:
            np, Image, Picamera2 = load_camera_stack()
            picam2 = Picamera2()
            config = picam2.create_video_configuration(
                main={"size": (width, height), "format": "RGB888"}
            )
            picam2.configure(config)
            if settings.manual_exposure:
                picam2.set_controls(build_camera_controls(settings.fps, settings.shutter_us, settings.analogue_gain))
            picam2.start()
            camera_started = True
            time.sleep(max(0.2, min(1.0, settings.shutter_sec or 0.35)))

            frame_args = RecordSettings(width=width, height=height, hflip=settings.hflip, vflip=settings.vflip)
            frame = prepare_frame(np, picam2.capture_array("main"), frame_args)
            output = BytesIO()
            Image.fromarray(frame).save(output, format="JPEG", quality=quality, optimize=True)
            return output.getvalue()
        finally:
            self._stop_camera(picam2, camera_started)
            self.camera_lock.release()

    def start_recording(self, settings: RecordSettings, action_callback: ActionCallback | None = None) -> dict[str, Any]:
        self._require_available()
        if not self.camera_lock.acquire(timeout=5.0):
            raise CameraBusyError("Pi camera is busy")

        job_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        with self.state_lock:
            self.last_error = None
            self.job = {
                "id": job_id,
                "status": "warming",
                "started_at": time.time(),
                "duration": settings.duration,
                "frame_count": 0,
                "settings": asdict(settings),
            }

        thread = threading.Thread(
            target=self._record_worker,
            args=(job_id, settings, action_callback),
            daemon=True,
        )
        thread.start()
        return self.get_state()

    def capture_path_for_request(self, request_path: str) -> Path | None:
        name = unquote(request_path.rsplit("/", 1)[-1]).replace("\\", "/")
        if not name or "/" in name:
            return None
        candidate = (self.capture_dir / name).resolve()
        try:
            candidate.relative_to(self.capture_dir.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def _record_worker(self, job_id: str, settings: RecordSettings, action_callback: ActionCallback | None) -> None:
        output_path = self.capture_dir / f"pi-long-exposure-{job_id}.jpg"
        picam2 = None
        camera_started = False

        try:
            np, Image, Picamera2 = load_camera_stack()
            picam2 = Picamera2()
            config = picam2.create_video_configuration(
                main={"size": (settings.width, settings.height), "format": "RGB888"}
            )
            picam2.configure(config)
            picam2.set_controls(build_camera_controls(settings.fps, settings.shutter_us, settings.analogue_gain))
            picam2.start()
            camera_started = True
            time.sleep(settings.warmup)

            if settings.trigger_lift and action_callback:
                action_callback("play_pause")

            with self.state_lock:
                if self.job and self.job.get("id") == job_id:
                    self.job["status"] = "recording"

            deadline = time.monotonic() + settings.duration
            accumulator = None
            frame_count = 0
            while time.monotonic() < deadline:
                frame = prepare_frame(np, picam2.capture_array("main"), settings)
                accumulator = blend_frame(np, accumulator, frame, settings.mode)
                frame_count += 1
                with self.state_lock:
                    if self.job and self.job.get("id") == job_id:
                        self.job["frame_count"] = frame_count

            output_array = finalize_image(np, accumulator, frame_count, settings.mode)
            save_image(Image, output_array, output_path, settings.quality)
            with self.state_lock:
                self.last_capture_path = output_path
                self.last_frame_count = frame_count
                self.last_error = None
        except Exception as exc:
            with self.state_lock:
                self.last_error = str(exc)
        finally:
            self._stop_camera(picam2, camera_started)
            if settings.stop_after and action_callback:
                try:
                    action_callback("stop")
                except Exception as exc:
                    with self.state_lock:
                        self.last_error = f"Stop after capture failed: {exc}"
            with self.state_lock:
                if self.job and self.job.get("id") == job_id:
                    self.job = None
            self.camera_lock.release()

    def _require_available(self) -> None:
        if not self.is_available():
            with self.state_lock:
                reason = self.detect_error or "No Pi camera detected"
            raise CameraUnavailableError(reason)

    def _format_camera_label(self, camera_info: Any) -> str:
        if isinstance(camera_info, dict):
            model = camera_info.get("Model") or camera_info.get("model")
            number = camera_info.get("Num") or camera_info.get("num")
            if model and number is not None:
                return f"{model} ({number})"
            if model:
                return str(model)
        return "Pi Camera"

    def _url_for_path(self, path: Path | None) -> str | None:
        if not path:
            return None
        return "/pi-camera/captures/" + quote(path.name)

    def _stop_camera(self, picam2: Any, camera_started: bool) -> None:
        if not picam2:
            return
        try:
            if camera_started:
                try:
                    picam2.stop()
                except Exception:
                    pass
        finally:
            try:
                picam2.close()
            except Exception:
                pass
