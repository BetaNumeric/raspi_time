"""Controller command regressions in simulation, with queued workers held."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import actuator_web as app


class ControllerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lift-controller-test-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        media = root / "media"
        media.mkdir()
        for replacement in (
            patch.object(app, "MEDIA_ROOT", media),
            patch.object(app, "STATE_FILE", root / "settings.json"),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.controller = app.InstallationController(None)
        self.addCleanup(self.controller.shutdown)
        self.queued = []

        def hold_worker(target, arguments, session):
            self.queued.append((target, arguments, session))

        self.launch_patch = patch.object(self.controller, "_launch_motion_worker", side_effect=hold_worker)
        self.launch_patch.start()
        self.addCleanup(self.launch_patch.stop)

    def start_jog(self, session_id="first-hold", direction=1):
        self.controller.start_jog(direction, session_id, self.controller.jog_generation, self.controller.jog_epoch)

    def assert_queued_work_stays_stopped(self, operation):
        checkpoint = len(self.queued)
        operation()
        self.assertEqual(len(self.queued), checkpoint + 1)
        target, arguments, session = self.queued[-1]
        self.assertTrue(self.controller.hardware.is_moving, "motion was not reserved before queueing")
        self.controller.stop_all()
        self.assertTrue(session.stop_event.is_set())
        errors = []

        def release():
            try:
                target(*arguments, session=session)
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=release, daemon=True)
        worker.start()
        worker.join(timeout=0.5)
        self.assertFalse(worker.is_alive(), "cancelled queued work restarted")
        self.assertEqual(errors, [])
        self.assertFalse(self.controller.hardware.is_moving)

    def test_stop_cancels_queued_target_movement(self):
        self.controller.target_pct = 50.0
        self.assert_queued_work_stays_stopped(self.controller.execute_movement)

    def test_stop_cancels_queued_browser_jog(self):
        self.assert_queued_work_stays_stopped(self.start_jog)

    def test_stop_cancels_queued_home(self):
        self.assert_queued_work_stays_stopped(self.controller.home)

    def test_stop_cancels_queued_physical_switch_movement(self):
        self.assert_queued_work_stays_stopped(lambda: self.controller.apply_switch_direction(1))

    def test_stop_cancels_a_cycle_reservation_before_motor_worker_enters(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.controller.hardware.move_diff
        sessions = []
        results = []
        self.controller.is_cycling = True

        def delayed_move(delta, duty, *, session):
            sessions.append(session)
            entered.set()
            release.wait(timeout=1.0)
            original(delta, duty, session=session)

        with patch.object(self.controller.hardware, "move_diff", side_effect=delayed_move):
            worker = threading.Thread(
                target=lambda: results.append(self.controller._cycle_move_to_target(
                    100.0, 1.0, self.controller.cycle_token
                )),
                daemon=True,
            )
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=0.5))
                self.controller.stop_all()
                self.assertTrue(sessions[0].stop_event.is_set())
            finally:
                release.set()
            worker.join(timeout=0.5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [False])
        self.assertFalse(self.controller.hardware.is_moving)

    def test_stop_invalidates_a_countdown_that_was_about_to_start(self):
        self.controller.target_pct = 50.0
        self.controller.start_countdown(3600.0)
        old_token = self.controller.countdown_token
        self.controller.stop_all()
        self.controller._execute_movement_from_countdown(old_token)
        self.controller._start_cycle_from_countdown(old_token)
        self.assertEqual(self.queued, [])
        self.assertFalse(self.controller.is_counting_down())
        self.assertFalse(self.controller.is_cycling)

    def test_stop_wins_over_a_countdown_request_already_in_progress(self):
        entered = threading.Event()
        release = threading.Event()
        stopping = threading.Event()
        original = self.controller.start_countdown

        def delayed_countdown(*args, **kwargs):
            entered.set()
            release.wait(timeout=1.0)
            original(*args, **kwargs)

        def stop():
            stopping.set()
            self.controller.stop_all()

        with patch.object(self.controller, "start_countdown", side_effect=delayed_countdown):
            starter = threading.Thread(target=self.controller.start_cycle_countdown, args=(120,), daemon=True)
            starter.start()
            try:
                self.assertTrue(entered.wait(timeout=0.5))
                stopper = threading.Thread(target=stop, daemon=True)
                stopper.start()
                self.assertTrue(stopping.wait(timeout=0.5))
                stopper.join(timeout=0.05)
            finally:
                release.set()
            starter.join(timeout=0.5)
            stopper.join(timeout=0.5)
        self.assertFalse(starter.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertFalse(self.controller.is_counting_down(), "Stop left a delayed countdown armed")

    def test_cycle_pause_and_subsequent_stop_do_not_deadlock(self):
        self.controller.is_cycling = True
        session = self.controller.hardware.reserve_motion()
        paused = threading.Thread(target=self.controller.play_pause, daemon=True)
        paused.start()
        paused.join(timeout=0.5)
        if paused.is_alive():
            # Do not block test-process cleanup on the very regression tested.
            self._cleanups = [item for item in self._cleanups if item[0] != self.controller.shutdown]
        self.assertFalse(paused.is_alive(), "cycle pause deadlocked")
        self.assertTrue(session.stop_event.is_set())
        self.assertTrue(self.controller.is_cycle_paused)
        self.assertFalse(self.controller.get_state()["is_moving"])
        self.controller.stop_all()
        self.assertFalse(self.controller.is_cycling)

    def test_early_release_rejects_a_delayed_start_request(self):
        generation = self.controller.jog_generation
        self.controller.stop_jog("released-before-start")
        with self.assertRaises(app.JogConflictError):
            self.controller.start_jog(1, "released-before-start", generation, self.controller.jog_epoch)
        self.assertEqual(self.queued, [])
        self.assertFalse(self.controller.hardware.is_moving)

    def test_stop_all_rejects_a_delayed_start_request(self):
        generation = self.controller.jog_generation
        self.controller.stop_all()
        with self.assertRaises(app.JogConflictError):
            self.controller.start_jog(1, "late-start", generation, self.controller.jog_epoch)
        self.assertEqual(self.queued, [])

    def test_old_hold_stop_and_heartbeat_do_not_affect_a_new_hold(self):
        self.start_jog("old-hold")
        self.controller.stop_jog("old-hold")
        self.start_jog("new-hold")
        current = self.queued[-1][2]
        self.controller.stop_jog("old-hold")
        with self.assertRaises(app.JogConflictError):
            self.controller.heartbeat_jog("old-hold")
        self.assertFalse(current.stop_event.is_set())
        self.assertTrue(self.controller.hardware.is_moving)
        self.controller.heartbeat_jog("new-hold")
        self.controller.stop_jog("new-hold")
        self.assertTrue(current.stop_event.is_set())

    def test_heartbeat_renews_only_an_unexpired_active_hold(self):
        self.start_jog()
        session = self.queued[-1][2]
        original_deadline = session.lease_deadline
        self.controller.heartbeat_jog("first-hold")
        self.assertGreaterEqual(session.lease_deadline, original_deadline)
        with patch.object(app.time, "monotonic", return_value=session.lease_deadline + 0.001):
            with self.assertRaises(app.JogConflictError):
                self.controller.heartbeat_jog("first-hold")
        self.assertTrue(session.stop_event.is_set())
        self.assertFalse(self.controller.hardware.is_moving)
        with self.assertRaises(app.JogConflictError):
            self.controller.heartbeat_jog("first-hold")

    def test_finished_jog_cannot_restart_before_its_controller_cleanup(self):
        self.start_jog()
        session = self.queued[-1][2]
        self.controller.hardware._finish_motion_session(session)
        # The worker has disabled GPIO but has not yet cleared its controller
        # identity. A duplicate/delayed Start must not exploit that interval.
        with self.assertRaises(app.JogConflictError):
            self.start_jog()
        self.assertEqual(len(self.queued), 1)
        self.assertFalse(self.controller.hardware.is_moving)
        self.controller.stop_jog("first-hold")
        self.start_jog("fresh-hold")
        self.assertEqual(len(self.queued), 2)

    def test_bad_or_missing_jog_identifiers_and_generations_cannot_start(self):
        for invalid_id in (None, "", 123, "x" * 129):
            with self.subTest(session_id=invalid_id), self.assertRaises(ValueError):
                self.controller.start_jog(1, invalid_id, self.controller.jog_generation, self.controller.jog_epoch)
        for invalid_direction in (0, 2, -2):
            with self.subTest(direction=invalid_direction), self.assertRaises(ValueError):
                self.start_jog(direction=invalid_direction)
        for invalid_generation in (None, True, "0", self.controller.jog_generation + 1):
            with self.subTest(generation=invalid_generation), self.assertRaises(app.JogConflictError):
                self.controller.start_jog(1, "hold", invalid_generation, self.controller.jog_epoch)
        self.assertEqual(self.queued, [])

    def test_old_server_epoch_cannot_start_jog_after_restart(self):
        with self.assertRaises(app.JogConflictError):
            self.controller.start_jog(1, "old-server-hold", self.controller.jog_generation, "stale-epoch")
        self.assertEqual(self.queued, [])
        self.assertFalse(self.controller.hardware.is_moving)

    def test_manual_jog_cancels_cycle_during_its_rest(self):
        self.controller.is_cycling = True
        token = self.controller.cycle_token
        self.start_jog()
        self.assertFalse(self.controller.is_cycling)
        self.assertNotEqual(self.controller.cycle_token, token)
        self.assertTrue(self.controller.hardware.is_moving)

    def test_old_browser_hold_cannot_stop_a_physical_switch_override(self):
        self.start_jog("old-browser-hold")
        self.controller.switch_stable_dir = 1
        self.controller.apply_switch_direction(1)
        switch_motion = self.queued[-1][2]
        self.controller.stop_jog("old-browser-hold")
        with self.assertRaises(app.JogConflictError):
            self.controller.heartbeat_jog("old-browser-hold")
        self.assertFalse(switch_motion.stop_event.is_set())
        self.assertTrue(self.controller.hardware.is_moving)
        with self.assertRaises(app.JogConflictError):
            self.start_jog("another-browser-hold")

    def test_shutdown_cancels_queued_work_and_latches_out_new_motion(self):
        self.start_jog()
        target, arguments, session = self.queued[-1]
        self.controller.shutdown()
        target(*arguments, session=session)
        self.assertTrue(session.stop_event.is_set())
        self.assertFalse(self.controller.hardware.is_moving)
        count = len(self.queued)
        self.controller.target_pct = 50.0
        self.controller.execute_movement()
        self.controller.home()
        self.controller.apply_switch_direction(1)
        self.assertEqual(len(self.queued), count)
        with self.assertRaises(app.JogConflictError):
            self.start_jog("after-shutdown")
        self.assertIsNone(self.controller.hardware.reserve_motion())

    def action_request(self, payload):
        handler = app.ActuatorRequestHandler.__new__(app.ActuatorRequestHandler)
        handler.controller = self.controller
        handler._read_json_body = Mock(return_value=payload)
        handler._controller_state = Mock(side_effect=self.controller.get_state)
        handler._send_json = Mock()
        handler._json_error = Mock()
        handler._handle_action_post()
        return handler

    def test_jog_api_starts_renews_and_releases_without_camera_work_on_heartbeat(self):
        start = self.action_request({
            "action": "jog_start", "direction": 1, "session_id": "api-hold",
            "generation": self.controller.jog_generation, "epoch": self.controller.jog_epoch,
        })
        start._json_error.assert_not_called()
        self.assertTrue(start._send_json.call_args.args[0]["state"]["is_moving"])
        session = self.queued[-1][2]
        heartbeat = self.action_request({"action": "jog_heartbeat", "session_id": "api-hold"})
        heartbeat._json_error.assert_not_called()
        heartbeat._send_json.assert_called_once_with({"ok": True})
        heartbeat._controller_state.assert_not_called()
        stop = self.action_request({"action": "jog_stop", "session_id": "api-hold"})
        stop._json_error.assert_not_called()
        self.assertTrue(session.stop_event.is_set())
        self.assertFalse(stop._send_json.call_args.args[0]["state"]["is_moving"])

    def test_jog_api_rejects_old_clients_and_expired_heartbeats(self):
        old_client = self.action_request({"action": "jog_start", "direction": 1})
        self.assertEqual(old_client._json_error.call_args.args[0], app.HTTPStatus.BAD_REQUEST)
        self.assertEqual(self.queued, [])
        expired = self.action_request({"action": "jog_heartbeat", "session_id": "old-hold"})
        self.assertEqual(expired._json_error.call_args.args[0], app.HTTPStatus.CONFLICT)
        expired._controller_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
