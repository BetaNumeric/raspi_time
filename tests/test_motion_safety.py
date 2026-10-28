"""Motor regressions using fake GPIO only; never request Raspberry Pi lines."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import actuator_web as app


OUTPUTS = (app.RPWM, app.LPWM, app.REN, app.LEN)
FAKE_GPIOD = SimpleNamespace(line=SimpleNamespace(Value=SimpleNamespace(ACTIVE=1, INACTIVE=0)))


class FakeGPIO:
    def __init__(self):
        self.values = dict.fromkeys(OUTPUTS, 0)
        self.writes = []
        self.fail_once = None
        self.lock = threading.Lock()

    def set_value(self, pin, value):
        with self.lock:
            self.writes.append((threading.current_thread().name, pin, value))
            if self.fail_once == (pin, value):
                self.fail_once = None
                raise OSError("injected GPIO failure")
            self.values[pin] = value


class MotionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.gpiod_patch = patch.object(app, "gpiod", FAKE_GPIOD)
        self.gpiod_patch.start()
        self.addCleanup(self.gpiod_patch.stop)
        self.gpio = FakeGPIO()
        self.hardware = app.ActuatorHardware(self.gpio)

    def worker(self, function, *args, **kwargs):
        errors = []

        def run():
            try:
                function(*args, **kwargs)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, errors

    def assert_outputs_off(self):
        self.assertEqual(self.gpio.values, dict.fromkeys(OUTPUTS, 0))

    def test_stop_cancels_all_kinds_of_queued_worker(self):
        calls = (
            (self.hardware.manual_move, (1, 1.0)),
            (self.hardware.move_diff, (25.0, 1.0)),
            (self.hardware.home, ()),
        )
        for function, arguments in calls:
            with self.subTest(worker=function.__name__):
                session = self.hardware.reserve_motion()
                self.hardware.stop_pwm()
                stopped_at = len(self.gpio.writes)
                thread, errors = self.worker(function, *arguments, session=session)
                thread.join(timeout=0.5)
                self.assertFalse(thread.is_alive(), "cancelled queued worker did not exit")
                self.assertEqual(errors, [])
                self.assertFalse(any(value for _, _, value in self.gpio.writes[stopped_at:]))
                self.assertFalse(self.hardware.is_moving)
                self.assert_outputs_off()

    def test_cancelled_home_does_not_fabricate_zero_position(self):
        self.hardware.position_pct = 47.0
        session = self.hardware.reserve_motion()
        self.hardware.stop_pwm()
        self.hardware.home(session=session)
        self.assertEqual(self.hardware.position_pct, 47.0)

    def test_no_op_movement_releases_its_reservation(self):
        for function in (self.hardware.move_diff, self.hardware.manual_move):
            with self.subTest(worker=function.__name__):
                session = self.hardware.reserve_motion()
                function(0, session=session)
                self.assertFalse(self.hardware.is_moving)
                self.assert_outputs_off()

    def test_expired_jog_session_stops_without_a_browser_stop(self):
        session = self.hardware.reserve_motion(lease_timeout=0.04)
        thread, errors = self.worker(self.hardware.manual_move, 1, 1.0, session=session)
        thread.join(timeout=0.5)
        self.assertFalse(thread.is_alive(), "jog ignored its expired lease")
        self.assertEqual(errors, [])
        self.assertFalse(self.hardware.is_moving)
        self.assert_outputs_off()

    def test_late_heartbeat_cannot_revive_an_expired_queued_jog(self):
        session = self.hardware.reserve_motion(lease_timeout=1.0)
        deadline = session.lease_deadline
        with patch.object(app.time, "monotonic", return_value=deadline + 0.001):
            self.assertFalse(self.hardware.renew_motion(session, 1.0))
            self.assertEqual(session.lease_deadline, deadline)
            checkpoint = len(self.gpio.writes)
            self.hardware.manual_move(1, session=session)
        self.assertFalse(any(value for _, _, value in self.gpio.writes[checkpoint:]))
        self.assert_outputs_off()

    def test_heartbeat_cannot_renew_replaced_or_finished_motion(self):
        old = self.hardware.reserve_motion(lease_timeout=1.0)
        current = self.hardware.reserve_motion(lease_timeout=1.0)
        self.assertFalse(self.hardware.renew_motion(old, 1.0))
        self.hardware.manual_move(0, session=current)
        self.assertFalse(self.hardware.renew_motion(current, 1.0))

    def test_replaced_worker_cannot_write_or_finalize_the_new_motion(self):
        old = self.hardware.reserve_motion()
        old_sleeping = threading.Event()
        release_old = threading.Event()
        new_sleeping = threading.Event()
        release_new = threading.Event()

        def old_wait(timeout=None):
            old_sleeping.set()
            release_old.wait(timeout=1.0)
            return old.stop_event.is_set()

        def new_wait(timeout=None):
            new_sleeping.set()
            release_new.wait(timeout=1.0)
            return new.stop_event.is_set()

        try:
            with patch.object(old.stop_event, "wait", side_effect=old_wait):
                old_thread, old_errors = self.worker(self.hardware.manual_move, 1, 1.0, session=old)
                self.assertTrue(old_sleeping.wait(timeout=0.5), "old worker did not reach PWM sleep")
                new = self.hardware.reserve_motion()
                with patch.object(new.stop_event, "wait", side_effect=new_wait):
                    new_thread, new_errors = self.worker(self.hardware.manual_move, -1, 1.0, session=new)
                    self.assertTrue(new_sleeping.wait(timeout=0.5), "new worker did not reach PWM sleep")
                    self.assertEqual(self.gpio.values[app.LPWM], 1)
                    position = self.hardware.position_pct
                    checkpoint = len(self.gpio.writes)
                    release_old.set()
                    old_thread.join(timeout=0.5)
                    self.assertFalse(old_thread.is_alive())
                    self.assertEqual(old_errors, [])
                    self.assertEqual(self.gpio.writes[checkpoint:], [], "stale worker touched GPIO")
                    self.assertEqual(self.hardware.position_pct, position)
                    self.assertTrue(self.hardware.is_moving)
                    self.hardware.stop_pwm()
                    release_new.set()
                    new_thread.join(timeout=0.5)
                    self.assertFalse(new_thread.is_alive())
                    self.assertEqual(new_errors, [])
        finally:
            release_old.set()
            release_new.set()
        self.assert_outputs_off()

    def test_setup_failures_still_disable_all_outputs(self):
        for pin in (app.REN, app.LEN, app.RPWM):
            with self.subTest(failing_pin=pin):
                session = self.hardware.reserve_motion()
                self.gpio.fail_once = (pin, 1)
                thread, errors = self.worker(self.hardware.manual_move, 1, 1.0, session=session)
                thread.join(timeout=0.5)
                self.assertFalse(thread.is_alive(), "setup error did not end the motion")
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], OSError)
                self.assertFalse(self.hardware.is_moving)
                self.assert_outputs_off()

    def test_stop_attempts_other_outputs_after_one_gpio_write_fails(self):
        session = self.hardware.reserve_motion()
        self.gpio.values = dict.fromkeys(OUTPUTS, 1)
        self.gpio.fail_once = (app.RPWM, 0)
        checkpoint = len(self.gpio.writes)
        try:
            self.hardware.stop_pwm()
        except OSError:
            pass  # Reporting the error is fine; skipping other outputs is not.
        self.assertTrue(session.stop_event.is_set())
        attempted_pins = {pin for _, pin, value in self.gpio.writes[checkpoint:] if value == 0}
        self.assertEqual(attempted_pins, set(OUTPUTS))
        for pin in (app.LPWM, app.REN, app.LEN):
            self.assertEqual(self.gpio.values[pin], 0)

    def test_motion_uses_monotonic_time_even_when_wall_clock_is_unusable(self):
        session = self.hardware.reserve_motion()
        with patch.object(app.time, "time", side_effect=AssertionError("wall clock used for motion")):
            thread, errors = self.worker(self.hardware.move_diff, 0.2, 1.0, session=session)
            thread.join(timeout=0.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertGreater(self.hardware.position_pct, 0.0)
        self.assertLess(self.hardware.position_pct, 1.0)
        self.assert_outputs_off()

    def test_home_uses_monotonic_timeout(self):
        self.hardware.position_pct = 47.0
        session = self.hardware.reserve_motion()
        with patch.object(app, "HOME_TIMEOUT_SEC", 0.02), patch.object(
            app.time, "time", side_effect=AssertionError("wall clock used for homing")
        ):
            thread, errors = self.worker(self.hardware.home, session=session)
            thread.join(timeout=0.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.hardware.position_pct, 0.0)
        self.assert_outputs_off()

    def test_close_permanently_rejects_new_reservations(self):
        session = self.hardware.reserve_motion()
        self.hardware.close()
        self.assertTrue(session.stop_event.is_set())
        self.assertIsNone(self.hardware.reserve_motion())
        self.assertFalse(self.hardware.is_moving)
        self.assert_outputs_off()

    def test_expired_queued_target_releases_its_reservation(self):
        session = self.hardware.reserve_motion(lease_timeout=1.0)
        with patch.object(app.time, "monotonic", return_value=session.lease_deadline + 1.0):
            self.hardware.move_diff(50.0, session=session)
        self.assertFalse(self.hardware.is_moving)
        self.assert_outputs_off()


if __name__ == "__main__":
    unittest.main()
