#!/usr/bin/env python3
"""Stop the local Time Volume controller before optionally powering off the Pi."""

import argparse
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / ".run" / "time_volume_server.pid"
SYSTEMD_RUNTIME = Path("/run/systemd/system")


def service_status(service: str) -> dict[str, str]:
    result = subprocess.run(
        ["systemctl", "show", "--property=LoadState,ActiveState,Result", "--", service],
        capture_output=True, text=True, timeout=10,
    )
    status = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if status.get("LoadState") == "not-found":
        return status
    if result.returncode or "ActiveState" not in status:
        raise RuntimeError(result.stderr.strip() or f"Could not inspect {service}")
    return status


def stop_service(service: str) -> None:
    if not SYSTEMD_RUNTIME.is_dir():
        return
    status = service_status(service)
    if status.get("LoadState") == "not-found" or status["ActiveState"] == "inactive":
        return
    print(f"Stopping {service}…", flush=True)
    subprocess.run(["systemctl", "stop", "--", service], check=True, timeout=100)
    status = service_status(service)
    if status["ActiveState"] != "inactive" or status.get("Result") != "success":
        raise RuntimeError(f"{service} did not report a clean stop. Check its systemd status.")


def stop_launcher_process() -> None:
    try:
        pid = int(PID_FILE.read_text().strip())
    except FileNotFoundError:
        return
    if pid <= 1:
        raise RuntimeError(f"Invalid controller PID in {PID_FILE}")
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return

    try:
        try:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            return
        # The launcher passes the absolute script path as Python's first argument.
        # A PID file can outlive the process, so verify the identity before signaling.
        if len(args) < 2:
            return
        script = Path(os.fsdecode(args[1]))
        if not script.is_absolute() or script.resolve() != ROOT / "actuator_web.py":
            return
        try:
            executable = Path(f"/proc/{pid}/exe").readlink()
        except FileNotFoundError:
            return
        if executable.name.split(".", 1)[0] not in {"python", "python3"}:
            return
        print(f"Stopping Time Volume controller (PID {pid})…", flush=True)
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        except ProcessLookupError:
            return
        poller = select.poll()
        poller.register(descriptor, select.POLLIN)
        if not poller.poll(30_000):
            raise RuntimeError("The controller did not exit within 30 seconds. Check its log.")
    finally:
        os.close(descriptor)


def stop_controller() -> None:
    service = os.environ.get("TIME_VOLUME_SERVICE_NAME", "time-volume.service")
    port = int(os.environ.get("TIME_VOLUME_PORT", "8000"))
    stop_service(service)
    stop_launcher_process()
    try:
        listener = socket.create_connection(("127.0.0.1", port), timeout=1)
    except ConnectionRefusedError:
        pass
    else:
        listener.close()
        raise RuntimeError(
            f"Port {port} is still in use. If you ran the controller directly, "
            "press Ctrl+C in its terminal and wait for it to exit."
        )
    print("Time Volume controller stopped.", flush=True)
    if (ROOT / "actuator_position.moving").exists():
        print("The saved lift position is still uncertain. Establish home when Time Volume restarts.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("quit", "poweroff"))
    parser.add_argument("--desktop", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.action == "poweroff":
            if input("Quit Time Volume and shut down this Pi? Type yes to continue: ").strip().lower() != "yes":
                print("Shutdown cancelled.")
                return 0
        stop_controller()
        if args.action == "poweroff":
            print("Requesting Pi shutdown. Wait for the Pi to finish shutting down before unplugging it.", flush=True)
            subprocess.run(["systemctl", "poweroff"], check=True, timeout=30)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, EOFError) as exc:
        print(f"Could not complete {args.action}: {exc}", file=sys.stderr, flush=True)
        if args.desktop and sys.stdin.isatty():
            input("Press Enter to close. ")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
