#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_CAPTURE_DIR = Path(__file__).resolve().parent / "captures"
ACTION_CHOICES = ("none", "play_pause", "cycle_toggle", "stop", "home")


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def quality_value(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return parsed


def threshold_value(value: str) -> int:
    parsed = int(value)
    if parsed < 0 or parsed > 255:
        raise argparse.ArgumentTypeError("must be between 0 and 255")
    return parsed


def default_output_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return DEFAULT_CAPTURE_DIR / f"pi-long-exposure-{stamp}.jpg"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture a Pi Camera frame stack and save it as a long-exposure-style image."
        )
    )
    parser.add_argument("--duration", type=positive_float, default=20.0, help="capture duration in seconds")
    parser.add_argument("--output", type=Path, default=None, help="output image path; defaults to captures/")
    parser.add_argument(
        "--mode",
        choices=("add", "lighten", "mean", "screen"),
        default="add",
        help="frame blend mode",
    )
    parser.add_argument("--width", type=positive_int, default=1280, help="capture width")
    parser.add_argument("--height", type=positive_int, default=720, help="capture height")
    parser.add_argument("--fps", type=positive_float, default=12.0, help="target capture frame rate")
    parser.add_argument("--warmup", type=non_negative_float, default=1.0, help="camera warmup seconds")
    parser.add_argument(
        "--start-delay",
        type=non_negative_float,
        default=0.0,
        help="seconds to wait after warmup before triggering/capturing",
    )
    parser.add_argument("--quality", type=quality_value, default=92, help="JPEG quality")
    parser.add_argument(
        "--threshold",
        type=threshold_value,
        default=0,
        help="zero out pixel channel values below this level before blending",
    )
    parser.add_argument("--hflip", action="store_true", help="flip frames horizontally")
    parser.add_argument("--vflip", action="store_true", help="flip frames vertically")
    parser.add_argument(
        "--shutter-us",
        type=positive_int,
        default=None,
        help="optional per-frame exposure time in microseconds",
    )
    parser.add_argument(
        "--gain",
        type=positive_float,
        default=None,
        help="optional analogue gain; disables auto exposure when set",
    )
    parser.add_argument(
        "--server-url",
        default=None,
        help="optional Time Volume server URL, for example http://127.0.0.1:8000",
    )
    parser.add_argument(
        "--start-action",
        choices=ACTION_CHOICES,
        default="none",
        help="optional actuator action to POST just before capture starts",
    )
    parser.add_argument(
        "--finish-action",
        choices=ACTION_CHOICES,
        default="none",
        help="optional actuator action to POST after capture finishes or is interrupted",
    )
    return parser


def load_camera_stack() -> tuple[Any, Any, Any]:
    missing: list[str] = []

    try:
        import numpy as np
    except Exception as exc:  # pragma: no cover - depends on Pi runtime packages
        missing.append(f"numpy ({exc})")

    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover - depends on Pi runtime packages
        missing.append(f"Pillow/python3-pil ({exc})")

    try:
        from picamera2 import Picamera2
    except Exception as exc:  # pragma: no cover - depends on Pi runtime packages
        missing.append(f"picamera2 ({exc})")

    if missing:
        raise RuntimeError(
            "Missing camera dependencies: "
            + ", ".join(missing)
            + ". Install them on Raspberry Pi OS with: "
            + "sudo apt install python3-picamera2 python3-numpy python3-pil"
        )

    return np, Image, Picamera2


def build_camera_controls(fps: float, shutter_us: int | None = None, gain: float | None = None) -> dict[str, Any]:
    controls: dict[str, Any] = {}
    if shutter_us is not None:
        frame_duration_us = max(int(1_000_000 / fps), int(shutter_us) + 1_000)
        controls["AeEnable"] = False
        controls["ExposureTime"] = int(shutter_us)
        controls["FrameDurationLimits"] = (frame_duration_us, frame_duration_us)
    else:
        controls["FrameRate"] = float(fps)

    if gain is not None:
        controls["AeEnable"] = False
        controls["AnalogueGain"] = float(gain)

    return controls


def post_action(server_url: str | None, action: str) -> None:
    if action == "none":
        return
    if not server_url:
        raise RuntimeError(f"--server-url is required when --start-action/--finish-action is {action!r}")

    url = server_url.rstrip("/") + "/api/action"
    body = json.dumps({"action": action}).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=5) as response:
            response.read(300)
    except HTTPError as exc:
        detail = exc.read(300).decode("utf-8", "replace")
        raise RuntimeError(f"{action} failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"{action} failed: {exc.reason}") from exc


def sleep_with_countdown(seconds: float, label: str) -> None:
    if seconds <= 0:
        return

    deadline = time.monotonic() + seconds
    last_tick: int | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        tick = int(math.ceil(remaining))
        if tick != last_tick:
            print(f"{label}: {tick}s", flush=True)
            last_tick = tick
        time.sleep(min(0.25, remaining))


def prepare_frame(np: Any, frame: Any, args: argparse.Namespace) -> Any:
    if frame.ndim == 2:
        frame = np.repeat(frame[:, :, None], 3, axis=2)
    elif frame.shape[2] > 3:
        frame = frame[:, :, :3]

    if args.hflip:
        frame = np.flip(frame, axis=1)
    if args.vflip:
        frame = np.flip(frame, axis=0)

    frame = np.ascontiguousarray(frame, dtype=np.uint8)
    if args.threshold > 0:
        frame = frame.copy()
        frame[frame < args.threshold] = 0
    return frame


def blend_frame(np: Any, accumulator: Any, frame: Any, mode: str) -> Any:
    if accumulator is None:
        if mode == "add":
            return frame.astype(np.float32)
        if mode == "lighten":
            return frame.copy()
        if mode == "mean":
            return frame.astype(np.float64)
        if mode == "screen":
            return frame.astype(np.float32) / 255.0
        raise ValueError(f"Unknown blend mode: {mode}")

    if mode == "add":
        accumulator += frame
        return accumulator
    if mode == "lighten":
        np.maximum(accumulator, frame, out=accumulator)
        return accumulator
    if mode == "mean":
        accumulator += frame
        return accumulator
    if mode == "screen":
        frame_float = frame.astype(np.float32) / 255.0
        accumulator[:] = 1.0 - ((1.0 - accumulator) * (1.0 - frame_float))
        return accumulator
    raise ValueError(f"Unknown blend mode: {mode}")


def finalize_image(np: Any, accumulator: Any, frame_count: int, mode: str) -> Any:
    if frame_count <= 0 or accumulator is None:
        raise RuntimeError("No frames were captured")

    if mode == "mean":
        output = accumulator / frame_count
    elif mode == "screen":
        output = accumulator * 255.0
    else:
        output = accumulator

    return np.clip(output, 0, 255).astype(np.uint8)


def save_image(Image: Any, output_array: Any, output_path: Path, quality: int) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not output_path.suffix:
        output_path = output_path.with_suffix(".jpg")

    image = Image.fromarray(output_array)
    suffix = output_path.suffix.lower()
    save_kwargs: dict[str, Any] = {}
    if suffix in {".jpg", ".jpeg"}:
        save_kwargs["quality"] = quality
        save_kwargs["optimize"] = True
    elif suffix == ".png":
        save_kwargs["compress_level"] = 6
    image.save(output_path, **save_kwargs)


def run_capture(args: argparse.Namespace) -> Path:
    np, Image, Picamera2 = load_camera_stack()
    output_path = args.output or default_output_path()
    if not output_path.suffix:
        output_path = output_path.with_suffix(".jpg")

    picam2 = Picamera2()
    config = picam2.create_video_configuration(
        main={"size": (args.width, args.height), "format": "RGB888"}
    )
    picam2.configure(config)

    picam2.set_controls(build_camera_controls(args.fps, args.shutter_us, args.gain))

    camera_started = False
    try:
        picam2.start()
        camera_started = True
        sleep_with_countdown(args.warmup, "Camera warmup")
        sleep_with_countdown(args.start_delay, "Capture starts in")

        post_action(args.server_url, args.start_action)

        print(
            f"Capturing {args.duration:.1f}s at {args.width}x{args.height}, "
            f"{args.fps:.1f} fps target, mode={args.mode}",
            flush=True,
        )
        deadline = time.monotonic() + args.duration
        next_report = time.monotonic() + 2.0
        accumulator = None
        frame_count = 0

        while True:
            now = time.monotonic()
            if now >= deadline:
                break

            frame = prepare_frame(np, picam2.capture_array("main"), args)
            accumulator = blend_frame(np, accumulator, frame, args.mode)
            frame_count += 1

            if now >= next_report:
                remaining = max(0.0, deadline - now)
                print(f"Captured {frame_count} frames, {remaining:.1f}s remaining", flush=True)
                next_report = now + 2.0

        output_array = finalize_image(np, accumulator, frame_count, args.mode)
        save_image(Image, output_array, output_path, args.quality)
        elapsed = args.duration
        print(f"Saved {output_path} from {frame_count} frames over {elapsed:.1f}s", flush=True)
        return output_path
    finally:
        stop_error: Exception | None = None
        if camera_started:
            try:
                picam2.stop()
            except Exception as exc:
                stop_error = exc

        if args.finish_action != "none":
            post_action(args.server_url, args.finish_action)
        if stop_error is not None:
            raise RuntimeError(f"Camera stop failed: {stop_error}") from stop_error


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.start_action != "none" or args.finish_action != "none") and not args.server_url:
        parser.error("--server-url is required when using --start-action or --finish-action")

    try:
        run_capture(args)
    except KeyboardInterrupt:
        print("Interrupted; no image was saved.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
