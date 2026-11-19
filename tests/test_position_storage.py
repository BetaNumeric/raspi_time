"""Power-loss recovery and GPIO ordering with temporary files and fake hardware."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import actuator_web as app
import state_storage as storage


class PositionStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lift-position-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = storage.PositionStore(self.root / "actuator_position.json")

    def test_clean_checkpoint_restores_position_and_playback_compensation(self):
        self.store.save_stopped(42.5, True, 0.75)
        hardware = app.ActuatorHardware(None, self.store)
        self.assertTrue(hardware.position_known)
        self.assertEqual(hardware.position_reference, "restored")
        self.assertEqual(hardware.position_pct, 42.5)
        self.assertEqual(hardware.playback_deadband_sec, 0.75)

    def test_missing_corrupt_or_checksum_damaged_latest_file_is_unknown(self):
        self.assertFalse(app.ActuatorHardware(None, self.store).position_known)
        self.store.save_stopped(10, True, 0)
        self.store.save_stopped(80, True, 0)
        self.assertEqual(self.store._read(self.store.backup)["position_pct"], 10)
        valid = self.store.path.read_bytes()
        for damaged in (b"{partial", b"[]", b"null"):
            self.store.path.write_bytes(damaged)
            self.assertFalse(app.ActuatorHardware(None, self.store).position_known)
        record = json.loads(valid)
        record["payload"]["position_pct"] = 12
        self.store.path.write_text(json.dumps(record))
        self.assertFalse(app.ActuatorHardware(None, self.store).position_known)
        self.store.path.unlink()
        self.assertFalse(app.ActuatorHardware(None, self.store).position_known)

    def test_abrupt_process_exit_after_dirty_marker_requires_home(self):
        source = """
import os, sys
from pathlib import Path
from state_storage import PositionStore
store = PositionStore(Path(sys.argv[1]))
store.save_stopped(37.0, True, 0.0)
store.begin_motion()
os._exit(0)
"""
        result = subprocess.run([sys.executable, "-B", "-c", source, str(self.store.path)], timeout=5)
        self.assertEqual(result.returncode, 0)
        hardware = app.ActuatorHardware(None, self.store)
        self.assertFalse(hardware.position_known)
        self.assertEqual(hardware.position_pct, 37)
        self.assertIn("interrupted", hardware.position_error)

    def test_failure_at_any_checkpoint_write_keeps_recovery_unknown(self):
        for fail_at in range(1, 7):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory(dir=self.root) as directory:
                store = storage.PositionStore(Path(directory) / "position.json")
                store.save_stopped(20, True, 0)
                store.begin_motion()
                original_sync = os.fsync
                count = 0

                def fail_sync(descriptor):
                    nonlocal count
                    count += 1
                    if count == fail_at:
                        raise OSError("simulated power-loss boundary")
                    original_sync(descriptor)

                # Backup sync, primary sync, then removal of dirty marker. A
                # failure after the new clean checkpoint is durable is safe too.
                with patch.object(storage.os, "fsync", side_effect=fail_sync):
                    try:
                        store.save_stopped(60, True, 0)
                    except OSError:
                        pass
                payload, error = store.load()
                self.assertTrue(error is not None or payload["position_pct"] == 60)
                self.assertFalse(any(Path(directory).glob("*.tmp")))

    def test_atomic_write_syncs_file_before_replace_and_directory_after(self):
        events = []
        original_sync, original_replace = os.fsync, os.replace

        def sync(descriptor):
            events.append("sync")
            original_sync(descriptor)

        def replace(source, target):
            events.append("replace")
            original_replace(source, target)

        with patch.object(storage.os, "fsync", side_effect=sync), patch.object(storage.os, "replace", side_effect=replace):
            self.store.begin_motion()
        self.assertEqual(events, ["sync", "replace", "sync"])

    def test_failed_atomic_replace_retains_previous_valid_primary(self):
        self.store.save_stopped(20, True, 0)
        self.store.begin_motion()
        with patch.object(storage.os, "replace", side_effect=OSError("storage unavailable")):
            with self.assertRaises(OSError):
                self.store.save_stopped(80, True, 0)
        self.assertEqual(self.store._read(self.store.path)["position_pct"], 20)
        self.assertTrue(self.store.pending.exists())

    def test_dirty_flag_is_durable_before_any_gpio_is_enabled(self):
        self.store.save_stopped(0, True, 0)
        req = Mock()
        hardware = app.ActuatorHardware(req, self.store)
        syncs = []
        original_sync = os.fsync

        def sync(descriptor):
            syncs.append(descriptor)
            original_sync(descriptor)

        def write(pin, value):
            if value == 1:
                self.assertTrue(self.store.pending.exists())
                self.assertGreaterEqual(len(syncs), 2)

        req.set_value.side_effect = write
        gpio = SimpleNamespace(line=SimpleNamespace(Value=SimpleNamespace(ACTIVE=1, INACTIVE=0)))
        with patch.object(app, "gpiod", gpio), patch.object(storage.os, "fsync", side_effect=sync):
            session = hardware.reserve_motion(direction=1)
            with patch.object(session.stop_event, "wait", side_effect=lambda seconds: hardware.stop_pwm()):
                hardware.manual_move(1, session=session)
        self.assertTrue(any(value == 1 for _, value in [call.args for call in req.set_value.call_args_list]))
        self.assertFalse(self.store.pending.exists())
        self.assertTrue(app.ActuatorHardware(None, self.store).position_known)

    def test_failed_dirty_write_prevents_motor_start(self):
        hardware = app.ActuatorHardware(None, self.store)
        hardware.enable = Mock()
        session = hardware.reserve_motion(direction=1)
        with patch.object(self.store, "begin_motion", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                hardware.manual_move(1, session=session)
        hardware.enable.assert_not_called()
        self.assertFalse(hardware.position_known)
        self.assertFalse(hardware.is_moving)

    def test_failed_stopped_write_leaves_dirty_marker_and_invalidates_estimate(self):
        self.store.save_stopped(0, True, 0)
        hardware = app.ActuatorHardware(None, self.store)
        self.store.begin_motion()
        hardware.position_dirty = True
        with patch.object(self.store, "save_stopped", side_effect=OSError("disk full")):
            hardware.stop_pwm()
        self.assertFalse(hardware.position_known)
        self.assertTrue(self.store.pending.exists())
        self.assertFalse(app.ActuatorHardware(None, self.store).position_known)

    def test_stop_and_shutdown_preserve_known_estimate_after_running(self):
        for stop in ("stop_pwm", "close"):
            with self.subTest(stop=stop):
                self.store.save_stopped(50, True, 0.5)
                hardware = app.ActuatorHardware(None, self.store)
                session = hardware.reserve_motion(direction=1)
                with patch.object(session.stop_event, "wait", side_effect=lambda seconds: getattr(hardware, stop)()):
                    hardware.manual_move(1, session=session)
                self.assertFalse(self.store.pending.exists())
                restored = app.ActuatorHardware(None, self.store)
                self.assertTrue(restored.position_known)
                self.assertEqual(restored.position_pct, 50)

    def test_disk_sync_time_is_not_counted_as_motor_running_time(self):
        self.store.save_stopped(0, True, 0)
        hardware = app.ActuatorHardware(None, self.store)
        session = hardware.reserve_motion(direction=1)
        clock = [0.0]
        original_begin = self.store.begin_motion

        def begin():
            original_begin()
            clock[0] += 10.0

        def wait(seconds):
            clock[0] += seconds

        with patch.object(self.store, "begin_motion", side_effect=begin), patch.object(
            app.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(session.stop_event, "wait", side_effect=wait):
            hardware.move_diff(0.2, session=session)
        self.assertLess(hardware.position_pct, 0.3)


class PositionRecoveryControllerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lift-recovery-test-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "media").mkdir()
        for replacement in (patch.object(app, "STATE_FILE", root / "actuator_state.json"), patch.object(app, "MEDIA_ROOT", root / "media")):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.controller = app.InstallationController(None)
        self.addCleanup(self.controller.shutdown)
        self.controller._launch_motion_worker = Mock()

    def test_manual_home_requires_confirmation_and_replaces_interrupted_checkpoint(self):
        controller = self.controller
        hardware = controller.hardware
        hardware.position_pct = 47
        hardware.playback_deadband_sec = 0.75
        hardware.position_store.save_stopped(47, True, 0.75)
        hardware.position_store.begin_motion()

        handler = app.ActuatorRequestHandler.__new__(app.ActuatorRequestHandler)
        handler.controller = controller
        handler._read_json_body = Mock(return_value={"action": "mark_home", "confirmed_at_bottom": "true"})
        handler._json_error = Mock()
        handler._send_json = Mock()
        handler._controller_state = controller.get_state
        handler._handle_action_post()
        handler._json_error.assert_called_once_with(app.HTTPStatus.BAD_REQUEST, "Confirm that the lift is fully lowered")
        self.assertTrue(hardware.position_store.pending.exists())

        handler._read_json_body.return_value["confirmed_at_bottom"] = True
        with patch.object(hardware, "enable") as enable:
            handler._handle_action_post()
            enable.assert_not_called()
        controller._launch_motion_worker.assert_not_called()
        state = handler._send_json.call_args.args[0]["state"]
        self.assertEqual(state["position_reference"], "manual_home")
        self.assertTrue(state["position_known"])
        self.assertFalse(state["is_moving"])
        self.assertEqual(state["position_pct"], 0)
        self.assertEqual(state["playback_deadband_sec"], 0)
        self.assertFalse(hardware.position_store.pending.exists())
        restored = app.ActuatorHardware(None, hardware.position_store)
        self.assertTrue(restored.position_known)
        self.assertEqual(restored.position_pct, 0)

    def test_manual_home_refuses_active_motion_cycle_countdown_or_switch(self):
        controller = self.controller
        for owner, attribute, value in (
            (controller.hardware, "is_moving", True),
            (controller, "is_cycling", True),
            (controller, "countdown_deadline", 123.0),
            (controller, "switch_stable_dir", -1),
        ):
            with self.subTest(attribute=attribute), patch.object(owner, attribute, value):
                with self.assertRaises(app.HomeConflictError):
                    controller.mark_home()
                self.assertFalse(controller.hardware.position_known)
                self.assertFalse(controller.hardware.position_store.path.exists())

    def test_manual_home_save_failure_does_not_publish_a_new_reference(self):
        hardware = self.controller.hardware
        hardware.position_pct = 47
        hardware.position_store.begin_motion()
        with patch.object(hardware.position_store, "save_stopped", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.controller.mark_home()
        self.assertFalse(hardware.position_known)
        self.assertEqual(hardware.position_pct, 47)
        self.assertTrue(hardware.position_store.pending.exists())

    def test_unknown_start_blocks_all_automatic_entry_points(self):
        self.controller.target_pct = 50
        for action in (self.controller.execute_movement, self.controller.toggle_cycle, lambda: self.controller.start_countdown(1)):
            with self.assertRaises(app.PositionUnknownError):
                action()
        self.controller.start_cycle_countdown(0)
        self.assertFalse(self.controller.is_cycling)
        self.assertFalse(self.controller.is_counting_down())
        self.controller._launch_motion_worker.assert_not_called()
        self.assertFalse(self.controller.get_state()["position_known"])
        self.assertEqual(self.controller.get_state()["qr_sync_display_mode"], "black")

    def test_manual_controls_remain_available_without_claiming_a_reference(self):
        self.controller.apply_switch_direction(1)
        self.assertTrue(self.controller.hardware.is_moving)
        self.controller.stop_all()
        self.controller.start_jog(-1, "manual", self.controller.jog_generation, self.controller.jog_epoch)
        self.assertTrue(self.controller.hardware.is_moving)
        self.assertFalse(self.controller.hardware.position_known)

    def test_queued_movement_is_black_until_durable_marker_preparation_finishes(self):
        self.controller.hardware.position_known = True
        self.controller.target_pct = 50
        self.controller.execute_movement()
        pending = self.controller.get_state()
        self.assertTrue(pending["motion_pending"])
        self.assertEqual(pending["qr_sync_display_mode"], "black")
        session = self.controller.hardware.motion_session
        observations = []

        def stop_after_start(seconds):
            observations.append(self.controller.get_state()["motion_pending"])
            self.controller.hardware.stop_pwm()

        with patch.object(session.stop_event, "wait", side_effect=stop_after_start):
            self.controller.hardware.manual_move(1, session=session)
        self.assertEqual(observations, [False])

    def test_clean_restart_restores_estimate_and_accepts_automatic_movement(self):
        self.controller.hardware.position_store.save_stopped(65, True, 0.75)
        restarted = app.InstallationController(None)
        self.addCleanup(restarted.shutdown)
        restarted._launch_motion_worker = Mock()
        state = restarted.get_state()
        self.assertTrue(state["position_known"])
        self.assertEqual(state["position_reference"], "restored")
        self.assertEqual(state["position_pct"], 65)
        restarted.set_settings({"target_pct": 80})
        restarted.execute_movement()
        restarted._launch_motion_worker.assert_called_once()

    def test_complete_home_creates_trusted_checkpoint_but_cancelled_home_does_not(self):
        self.controller.home()
        session = self.controller._launch_motion_worker.call_args.args[2]
        with patch.object(app, "HOME_TIMEOUT_SEC", 0.01):
            self.controller.hardware.home(session=session)
        self.assertTrue(self.controller.get_state()["position_known"])
        self.assertEqual(self.controller.hardware.position_pct, 0)
        self.assertTrue(app.ActuatorHardware(None, self.controller.hardware.position_store).position_known)
        self.controller.home()
        session = self.controller._launch_motion_worker.call_args.args[2]
        with patch.object(session.stop_event, "wait", side_effect=lambda seconds: self.controller.hardware.stop_pwm()):
            self.controller.hardware.home(session=session)
        self.assertFalse(self.controller.hardware.position_known)
        self.assertFalse(app.ActuatorHardware(None, self.controller.hardware.position_store).position_known)

    def test_settings_recover_from_backup_without_rewriting_damaged_primary(self):
        self.controller.set_settings({"speed_duty": 0.5})
        self.controller._save_settings()
        self.controller.set_settings({"speed_duty": 0.7})
        self.controller._save_settings()
        app.STATE_FILE.write_text("{corrupt")
        self.controller._load_settings()
        self.assertEqual(self.controller.speed_duty, 0.5)
        self.assertEqual(app.STATE_FILE.read_text(), "{corrupt")

    def test_simulation_does_not_restore_or_overwrite_physical_position(self):
        physical = storage.PositionStore(app.STATE_FILE.with_name("actuator_position.json"))
        physical.save_stopped(75, True, 0)
        self.assertNotEqual(physical.path, self.controller.hardware.position_store.path)
        self.assertFalse(self.controller.hardware.position_known)
        self.controller.shutdown()
        self.assertEqual(physical.load()[0]["position_pct"], 75)


if __name__ == "__main__":
    unittest.main()
