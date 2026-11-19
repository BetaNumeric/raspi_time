"""Durable local state writes and conservative recovery of lift position."""

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, body: bytes) -> None:
    """Sync contents, replace atomically, then sync the directory entry."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("State must be a JSON object")
    return data


class PositionStore:
    def __init__(self, path: Path):
        self.path = path
        self.backup = path.with_suffix(".json.bak")
        self.pending = path.with_suffix(".moving")

    @staticmethod
    def _body(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    def _read(self, path: Path) -> dict[str, Any]:
        record = read_json_object(path)
        payload = record["payload"]
        if not isinstance(payload, dict) or record.get("sha256") != hashlib.sha256(self._body(payload)).hexdigest():
            raise ValueError("Position checksum mismatch")
        if payload.get("version") != 1 or type(payload.get("known")) is not bool:
            raise ValueError("Unsupported position record")
        for key, minimum, maximum in (("position_pct", 0, 100), ("playback_deadband_sec", 0, 5)):
            value = payload.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError(f"Invalid {key}")
        return payload

    def load(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            payload = self._read(self.path)
        except (OSError, ValueError, KeyError, TypeError):
            # A backup may be an older height. Keep it for recovery/inspection,
            # but never silently restore it as the lift's current position.
            return None, "Position checkpoint missing or damaged; Home required"
        if self.pending.exists():
            return payload, "Movement was interrupted; Home required"
        if not payload["known"]:
            return payload, "Position is unknown; Home required"
        return payload, None

    def begin_motion(self) -> None:
        # The directory entry must be durable before any motor output is enabled.
        atomic_write(self.pending, b"Movement in progress; Home required after an interrupted run.\n")

    def save_stopped(self, position_pct: float, known: bool, playback_deadband_sec: float) -> None:
        payload = {
            "version": 1, "position_pct": position_pct, "known": known,
            "playback_deadband_sec": playback_deadband_sec,
        }
        record = self._body({"payload": payload, "sha256": hashlib.sha256(self._body(payload)).hexdigest()}) + b"\n"
        try:
            self._read(self.path)
        except (OSError, ValueError, KeyError, TypeError):
            # Preserve an existing backup if the latest record is damaged.
            if not self.backup.exists():
                atomic_write(self.backup, record)
        else:
            atomic_write(self.backup, self.path.read_bytes())
        atomic_write(self.path, record)
        # Only a durable stopped record makes it safe to remove the dirty flag.
        self.pending.unlink(missing_ok=True)
        sync_directory(self.path.parent)
