import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import actuator_web as aw


class IconRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lift-icons-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "icons").mkdir()
        (self.root / "icons" / "normal.png").write_bytes(b"icon")
        (self.root / "outside.txt").write_text("outside icon directory")
        self.handler = aw.ActuatorRequestHandler.__new__(aw.ActuatorRequestHandler)
        self.handler._serve_file = Mock()
        self.handler._json_error = Mock()
        self.patch = patch.object(aw, "BASE_DIR", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def route(self, path):
        self.handler.path = path
        self.handler._serve_file.reset_mock()
        self.handler._json_error.reset_mock()
        self.handler._handle_request(head_only=True)

    def test_existing_icon_is_served(self):
        self.route("/icons/normal.png")
        self.handler._serve_file.assert_called_once_with(
            self.root / "icons" / "normal.png",
            cache_control="public, max-age=86400",
            head_only=True,
        )
        self.handler._json_error.assert_not_called()

    def test_parent_absolute_and_encoded_paths_are_rejected(self):
        for path in (
            "/icons/../outside.txt",
            "/icons/..%2Foutside.txt",
            "/icons/..%5Coutside.txt",
            "/icons/" + str(self.root / "outside.txt"),
        ):
            with self.subTest(path=path):
                self.route(path)
                self.handler._serve_file.assert_not_called()
                self.handler._json_error.assert_called_once_with(aw.HTTPStatus.NOT_FOUND, "Icon not found")

    def test_symlink_to_outside_directory_is_rejected(self):
        (self.root / "icons" / "link.png").symlink_to(self.root / "outside.txt")
        self.route("/icons/link.png")
        self.handler._serve_file.assert_not_called()
        self.handler._json_error.assert_called_once_with(aw.HTTPStatus.NOT_FOUND, "Icon not found")


class ApplicationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="lift-lifecycle-test-"))
        root = Path(self.directory)
        self.stack.enter_context(patch.object(aw, "MEDIA_ROOT", root / "media"))
        self.stack.enter_context(patch.object(aw, "WEB_DIR", root / "web"))
        self.stack.enter_context(patch.object(aw, "PiCameraRuntime", None))
        self.stack.enter_context(patch.object(aw, "AUTO_START_CYCLE_ON_BOOT", False))
        self.stack.enter_context(patch.object(aw, "print_launch_hints"))
        self.args = SimpleNamespace(
            host="127.0.0.1", port=0, require_gpio=True,
            display_backend="none", mpv_bin="mpv", mpv_fps_cap=0,
        )
        parser = Mock()
        parser.parse_args.return_value = self.args
        self.stack.enter_context(patch.object(aw, "build_parser", return_value=parser))
        self.gpio = Mock(spec=["set_value", "release"])
        self.request_gpio = self.stack.enter_context(patch.object(aw, "request_gpio_lines", return_value=self.gpio))
        self.controller = Mock()
        self.construct_controller = self.stack.enter_context(
            patch.object(aw, "InstallationController", return_value=self.controller)
        )
        self.server = Mock(spec=["handle_request", "server_close"])
        self.construct_server = self.stack.enter_context(
            patch.object(aw, "ReusableThreadingHTTPServer", return_value=self.server)
        )
        self.stack.enter_context(patch.object(aw, "gpiod", SimpleNamespace(
            line=SimpleNamespace(Value=SimpleNamespace(INACTIVE=0)),
        )))
        self.original_handlers = {signal.SIGINT: object(), signal.SIGTERM: object()}
        self.handlers = dict(self.original_handlers)

        def install_handler(signum, handler):
            previous = self.handlers[signum]
            self.handlers[signum] = handler
            return previous

        self.stack.enter_context(patch.object(aw.signal, "signal", side_effect=install_handler))

        def terminate():
            self.handlers[signal.SIGTERM](signal.SIGTERM, None)
            # Cleanup is deferred to normal execution, never the handler.
            self.controller.shutdown.assert_not_called()

        self.server.handle_request.side_effect = terminate

    def assert_gpio_released_safely(self):
        self.assertEqual(self.gpio.set_value.call_args_list, [
            call(aw.REN, 0), call(aw.LEN, 0), call(aw.RPWM, 0), call(aw.LPWM, 0),
        ])
        self.gpio.release.assert_called_once_with()

    def assert_handlers_restored(self):
        self.assertEqual(self.handlers, self.original_handlers)

    def test_sigterm_runs_cleanup_and_restores_signal_handlers(self):
        aw.main()
        self.controller.shutdown.assert_called_once_with()
        self.server.server_close.assert_called_once_with()
        self.assert_gpio_released_safely()
        self.assert_handlers_restored()
        self.assertIsNone(aw.ActuatorRequestHandler.controller)
        self.assertIsNone(aw.ActuatorRequestHandler.pi_camera)

    def test_sigint_runs_cleanup(self):
        self.server.handle_request.side_effect = lambda: self.handlers[signal.SIGINT](signal.SIGINT, None)
        aw.main()
        self.controller.shutdown.assert_called_once_with()
        self.assert_gpio_released_safely()
        self.assert_handlers_restored()

    def test_occupied_port_does_not_start_gpio_or_controller(self):
        self.construct_server.side_effect = OSError("occupied port")
        with self.assertRaisesRegex(OSError, "occupied port"):
            aw.main()
        self.request_gpio.assert_not_called()
        self.construct_controller.assert_not_called()
        self.assert_handlers_restored()

    def test_gpio_request_failure_closes_server(self):
        self.request_gpio.side_effect = RuntimeError("GPIO unavailable")
        with self.assertRaisesRegex(RuntimeError, "GPIO unavailable"):
            aw.main()
        self.server.server_close.assert_called_once_with()
        self.construct_controller.assert_not_called()
        self.assert_handlers_restored()

    def test_controller_construction_failure_disables_and_releases_gpio(self):
        self.construct_controller.side_effect = RuntimeError("construction failed")
        with self.assertRaisesRegex(RuntimeError, "construction failed"):
            aw.main()
        self.assert_gpio_released_safely()
        self.server.server_close.assert_called_once_with()
        self.assert_handlers_restored()

    def test_display_startup_failure_runs_cleanup(self):
        self.args.display_backend = "mpv"
        self.controller.start_mpv_display.side_effect = RuntimeError("display failed")
        with self.assertRaisesRegex(RuntimeError, "display failed"):
            aw.main()
        self.controller.shutdown.assert_called_once_with()
        self.assert_gpio_released_safely()
        self.server.server_close.assert_called_once_with()

    def test_shutdown_failure_still_disables_and_releases_gpio(self):
        self.controller.shutdown.side_effect = RuntimeError("shutdown failed")
        aw.main()
        self.assert_gpio_released_safely()
        self.server.server_close.assert_called_once_with()
        self.assert_handlers_restored()

    def test_one_output_error_does_not_skip_other_outputs_or_release(self):
        self.gpio.set_value.side_effect = [OSError("write failed"), None, None, None]
        aw.main()
        self.assert_gpio_released_safely()

    def test_signal_during_gpio_request_prevents_controller_startup(self):
        def request_gpio(**kwargs):
            self.handlers[signal.SIGTERM](signal.SIGTERM, None)
            return self.gpio
        self.request_gpio.side_effect = request_gpio
        aw.main()
        self.construct_controller.assert_not_called()
        self.assert_gpio_released_safely()
        self.server.server_close.assert_called_once_with()
        self.assert_handlers_restored()

    def test_actual_sigterm_in_subprocess_runs_cleanup_and_exits_normally(self):
        # Keep the real signal mechanism while replacing every hardware,
        # display and network operation. The child never opens a GPIO/socket.
        source = textwrap.dedent('''
            import os
            import signal
            import sys
            from pathlib import Path
            from types import SimpleNamespace
            import actuator_web as aw

            root = Path(sys.argv[1])
            sys.argv = ["review", "--display-backend", "none"]
            aw.MEDIA_ROOT = root / "child-media"
            aw.WEB_DIR = root / "child-web"
            aw.PiCameraRuntime = None
            aw.AUTO_START_CYCLE_ON_BOOT = False
            aw.print_launch_hints = lambda *args: None
            aw.gpiod = SimpleNamespace(line=SimpleNamespace(Value=SimpleNamespace(INACTIVE=0)))
            aw.request_gpio_lines = lambda **kwargs: SimpleNamespace(
                set_value=lambda line, value: print(f"DISABLE:{line}:{value}", flush=True),
                release=lambda: print("GPIO_RELEASED", flush=True),
            )

            class Controller:
                def __init__(self, *args, **kwargs): pass
                def set_display_backend_status(self, *args): pass
                def shutdown(self): print("CONTROLLER_SHUTDOWN", flush=True)

            class Server:
                def __init__(self, *args, **kwargs): pass
                def handle_request(self):
                    os.kill(os.getpid(), signal.SIGTERM)
                    print("SIGNAL_HANDLED", flush=True)
                def server_close(self): print("SERVER_CLOSED", flush=True)

            aw.InstallationController = Controller
            aw.ReusableThreadingHTTPServer = Server
            aw.main()
            print("CLEAN_EXIT", flush=True)
        ''')
        result = subprocess.run(
            [sys.executable, "-B", "-c", source, self.directory],
            cwd=aw.BASE_DIR, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "SIGNAL_HANDLED", "CONTROLLER_SHUTDOWN",
            f"DISABLE:{aw.REN}:0", f"DISABLE:{aw.LEN}:0",
            f"DISABLE:{aw.RPWM}:0", f"DISABLE:{aw.LPWM}:0",
            "GPIO_RELEASED", "SERVER_CLOSED", "CLEAN_EXIT",
        ])


if __name__ == "__main__":
    unittest.main()
