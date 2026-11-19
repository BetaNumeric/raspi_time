"""Local shutdown checks using fake systemd calls and a private child process."""

import os
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import shutdown_time_volume as shutdown
import actuator_web as app


class ControllerPoweroffTests(unittest.TestCase):
    def test_http_route_requires_configuration_https_key_and_confirmation(self):
        token = "a" * 43
        for configured, peer, scheme, payload, expected in (
            (None, "127.0.0.1", "https", {"token": token, "confirmed": True}, 404),
            (token, "192.0.2.1", "https", {"token": token, "confirmed": True}, 403),
            (token, "127.0.0.1", "http", {"token": token, "confirmed": True}, 403),
            (token, "127.0.0.1", "https", {"token": "wrong", "confirmed": True}, 403),
            (token, "127.0.0.1", "https", {"token": token, "confirmed": False}, 400),
            (token, "127.0.0.1", "https", {"token": token, "confirmed": True}, 202),
        ):
            with self.subTest(configured=bool(configured), peer=peer, scheme=scheme, expected=expected):
                handler = app.ActuatorRequestHandler.__new__(app.ActuatorRequestHandler)
                handler.server = SimpleNamespace(poweroff_token=configured)
                handler.controller = Mock()
                handler.client_address = (peer, 8000)
                handler.path = "/api/poweroff"
                handler.headers = Message()
                body = json.dumps(payload).encode()
                handler.headers["Content-Length"] = str(len(body))
                handler.headers["Content-Type"] = "application/json"
                handler.headers["X-Forwarded-Proto"] = scheme
                handler.rfile = BytesIO(body)
                handler._send_json = Mock()
                handler.do_POST()
                reply = handler._send_json.call_args
                status = reply.kwargs["status"] if "status" in reply.kwargs else reply.args[1]
                self.assertEqual(status, expected)
                if expected == 202:
                    handler.controller.request_poweroff.assert_called_once_with()
                else:
                    handler.controller.request_poweroff.assert_not_called()

    def test_only_private_well_formed_token_files_enable_poweroff(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            self.assertIsNone(app.load_poweroff_token(path))
            path.write_text("a" * 43 + "\n")
            path.chmod(0o644)
            with patch("builtins.print"):
                self.assertIsNone(app.load_poweroff_token(path))
            path.chmod(0o600)
            self.assertEqual(app.load_poweroff_token(path), "a" * 43)

    def test_controller_stops_before_poweroff_and_blocks_rearming(self):
        controller = app.InstallationController.__new__(app.InstallationController)
        controller.command_lock = threading.RLock()
        controller.shutdown_event = threading.Event()
        controller.hardware = SimpleNamespace(req=object())
        actions = Mock()
        controller.stop_all = actions.stop
        command = ["/usr/bin/sudo", "-n", "--", "/usr/bin/systemctl", "poweroff"]

        with patch.object(app.subprocess, "run", actions.run):
            actions.stop.side_effect = OSError("GPIO failed")
            with self.assertRaises(OSError):
                controller.request_poweroff()
            actions.run.assert_not_called()
            self.assertFalse(controller.shutdown_event.is_set())
            actions.reset_mock()
            actions.stop.side_effect = None

            actions.run.side_effect = subprocess.CalledProcessError(1, command)
            with patch("builtins.print"), self.assertRaisesRegex(RuntimeError, "Could not confirm Pi shutdown"):
                controller.request_poweroff()
            self.assertFalse(controller.shutdown_event.is_set())
            actions.reset_mock()
            actions.run.side_effect = None

            controller.request_poweroff()
            self.assertEqual([call[0] for call in actions.mock_calls], ["stop", "run"])
            actions.run.assert_called_once_with(command, check=True, timeout=10, capture_output=True)
            self.assertTrue(controller.shutdown_event.is_set())
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                controller.request_poweroff()
            actions.run.assert_called_once()


class ShutdownCommandTests(unittest.TestCase):
    def run_command(self, action, answer="yes", stop_error=None):
        with (
            patch.object(sys, "argv", ["shutdown_time_volume.py", action]),
            patch("builtins.input", return_value=answer),
            patch.object(shutdown, "stop_controller", side_effect=stop_error) as stop_controller,
            patch.object(shutdown.subprocess, "run") as system_command,
        ):
            result = shutdown.main()
        return result, stop_controller, system_command

    def test_quit_does_not_power_off(self):
        result, stop_controller, system_command = self.run_command("quit")
        self.assertEqual(result, 0)
        stop_controller.assert_called_once_with()
        system_command.assert_not_called()

    def test_declined_poweroff_does_not_stop_controller(self):
        result, stop_controller, system_command = self.run_command("poweroff", answer="no")
        self.assertEqual(result, 0)
        stop_controller.assert_not_called()
        system_command.assert_not_called()

    def test_failed_stop_prevents_poweroff(self):
        with patch.object(sys, "stderr"):
            result, stop_controller, system_command = self.run_command("poweroff", stop_error=RuntimeError("still running"))
        self.assertEqual(result, 1)
        stop_controller.assert_called_once_with()
        system_command.assert_not_called()

    def test_poweroff_is_requested_after_controller_stops(self):
        operations = Mock()
        with (
            patch.object(sys, "argv", ["shutdown_time_volume.py", "poweroff"]),
            patch("builtins.input", return_value="yes"),
            patch.object(shutdown, "stop_controller", operations.stop),
            patch.object(shutdown.subprocess, "run", operations.run),
        ):
            self.assertEqual(shutdown.main(), 0)
        self.assertEqual([call[0] for call in operations.mock_calls], ["stop", "run"])
        operations.run.assert_called_once_with(["systemctl", "poweroff"], check=True, timeout=30)

    def test_systemd_stop_timeout_result_is_rejected(self):
        with (
            patch.object(shutdown, "SYSTEMD_RUNTIME", Mock(is_dir=Mock(return_value=True))),
            patch.object(shutdown, "service_status", side_effect=[
                {"LoadState": "loaded", "ActiveState": "active"},
                {"LoadState": "loaded", "ActiveState": "inactive", "Result": "timeout"},
            ]),
            patch.object(shutdown.subprocess, "run") as system_command,
        ):
            with self.assertRaisesRegex(RuntimeError, "did not report a clean stop"):
                shutdown.stop_service("time-volume.service")
        system_command.assert_called_once_with(["systemctl", "stop", "--", "time-volume.service"], check=True, timeout=100)


@unittest.skipUnless(hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"), "requires Linux pidfds")
class LauncherProcessTests(unittest.TestCase):
    def test_stop_waits_for_real_signal_handler_and_ignores_unrelated_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "actuator_web.py"
            ready, saved = root / "ready", root / "saved"
            script.write_text(
                "import signal, sys, time\n"
                "from pathlib import Path\n"
                "def stop(*args):\n"
                "    time.sleep(0.1)\n"
                "    Path(sys.argv[2]).write_text('saved')\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "Path(sys.argv[1]).touch()\n"
                "while True:\n"
                "    time.sleep(0.1)\n"
            )
            pid_file = root / "controller.pid"
            with subprocess.Popen([sys.executable, script, ready, saved]) as child:
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists(), "private controller did not start")
                    pid_file.write_text(str(child.pid))
                    with patch.object(shutdown, "PID_FILE", pid_file):
                        with patch.object(shutdown, "ROOT", root / "unrelated"):
                            shutdown.stop_launcher_process()
                        self.assertIsNone(child.poll())
                        with patch.object(shutdown, "ROOT", root):
                            shutdown.stop_launcher_process()
                    self.assertEqual(saved.read_text(), "saved")
                    self.assertEqual(child.wait(timeout=1), 0)
                finally:
                    if child.poll() is None:
                        child.kill()
