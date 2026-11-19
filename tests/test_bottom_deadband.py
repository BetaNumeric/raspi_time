"""Bottom playback compensation without requesting GPIO or starting MPV."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import actuator_web as app


class BottomDeadbandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="lift-deadband-test-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        media = root / "media"
        media.mkdir()
        for replacement in (
            patch.object(app, "MEDIA_ROOT", media),
            patch.object(app, "STATE_FILE", root / "settings.json"),
            patch.object(app, "RUN_DIR", root / "run"),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.controller = app.InstallationController(None)
        self.controller.hardware.position_known = True
        self.controller.hardware.position_error = None
        self.addCleanup(self.controller.shutdown)
        self.controller._launch_motion_worker = Mock()
        self.sequence = app.SequenceItem(
            id="test", name="test", kind="images", relative_path="test",
            frame_count=101, frame_paths=[f"test/{index}.png" for index in range(101)],
        )

    def start_up(self, delay=1.0):
        self.controller.set_settings({"bottom_deadband_sec": delay, "target_pct": 100})
        self.controller.execute_movement()
        return self.controller.get_state()

    def test_setting_defaults_off_and_survives_restart(self):
        self.assertEqual(self.controller.get_state()["bottom_deadband_sec"], 0)
        self.controller.set_settings({"bottom_deadband_sec": 0.65})
        self.controller._save_settings()
        self.assertEqual(json.loads(app.STATE_FILE.read_text())["bottom_deadband_sec"], 0.65)
        restarted = app.InstallationController(None)
        self.addCleanup(restarted.shutdown)
        self.assertEqual(restarted.get_state()["bottom_deadband_sec"], 0.65)

    def test_invalid_values_are_rejected_and_old_invalid_state_loads_safely(self):
        for value in (-0.1, 5.1, "invalid", float("nan"), float("inf"), None):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                self.controller.set_settings({"bottom_deadband_sec": value})
        self.assertEqual(self.controller.bottom_deadband_sec, 0)
        app.STATE_FILE.write_text('{"bottom_deadband_sec": "invalid"}')
        self.controller._load_settings()
        self.assertEqual(self.controller.bottom_deadband_sec, 0)

    def test_upstroke_is_black_before_delay_then_spans_all_frames(self):
        self.assertTrue(self.start_up()["playback_deadband_active"])
        with patch.object(self.controller.library, "get", return_value=self.sequence):
            for time_at_full_speed, expected_position, expected_frame in (
                (0.5, 0.0, None),
                (1.0, 0.0, 0),
                (1.0 + (app.EXTEND_SEC - 1.0) / 2, 50.0, 50),
                (app.EXTEND_SEC, 100.0, 100),
            ):
                self.controller.hardware.position_pct = time_at_full_speed * 100 / app.EXTEND_SEC
                state = self.controller.get_state()
                self.assertAlmostEqual(state["playback_position_pct"], expected_position)
                self.assertEqual(state["frame_index"], expected_frame)
                self.assertEqual(state["extend_sec"], app.EXTEND_SEC)

    def test_pause_resume_preserves_compensation_and_changes_apply_to_next_upstroke(self):
        self.start_up()
        self.controller.hardware.position_pct = 10.0
        self.controller.hardware.pause()
        self.controller.set_settings({"bottom_deadband_sec": 2.0})
        self.controller.execute_movement()
        self.assertEqual(self.controller.get_state()["playback_deadband_sec"], 1.0)
        self.controller.stop_all()
        self.controller.apply_switch_direction(-1)
        self.controller.hardware.position_pct = 0
        self.controller.stop_all()
        self.controller.execute_movement()
        self.assertEqual(self.controller.get_state()["playback_deadband_sec"], 2.0)

    def test_descent_and_midstroke_start_have_no_bottom_delay(self):
        self.controller.bottom_deadband_sec = 1.0
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                self.controller.hardware.position_pct = 50.0
                self.controller.apply_switch_direction(direction)
                state = self.controller.get_state()
                self.assertFalse(state["playback_deadband_active"])
                self.assertEqual(state["playback_deadband_sec"], 0)
                self.assertEqual(state["playback_position_pct"], 50.0)
                self.controller.stop_all()

    def test_jog_and_physical_switch_reserve_the_same_upstroke_compensation(self):
        self.controller.bottom_deadband_sec = 0.75
        self.controller.start_jog(1, "hold", self.controller.jog_generation, self.controller.jog_epoch)
        self.assertTrue(self.controller.get_state()["playback_deadband_active"])
        self.controller.stop_all()
        self.controller.apply_switch_direction(1)
        state = self.controller.get_state()
        self.assertTrue(state["playback_deadband_active"])
        self.assertEqual(state["motion_direction"], 1)

    def test_compensation_does_not_change_motor_run_time(self):
        elapsed = []
        for delay in (0.0, 1.0):
            hardware = app.ActuatorHardware(None)
            session = hardware.reserve_motion(direction=1, bottom_deadband_sec=delay)
            clock = [0.0]

            def wait(seconds):
                clock[0] += seconds
                return False

            with patch.object(app.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
                session.stop_event, "wait", side_effect=wait
            ):
                hardware.move_diff(100, session=session)
            elapsed.append(clock[0])
            self.assertGreater(hardware.position_pct, 99.9)
        self.assertEqual(elapsed[0], elapsed[1])

    def test_mpv_blacks_out_deadband_then_uses_compensated_position(self):
        self.start_up()
        self.controller.hardware.motion_session.started = True
        backend = app.MpvDisplayBackend(self.controller)
        backend._load_black = Mock()
        backend._sync_images = Mock()
        backend._sync_video = Mock()
        with patch.object(self.controller.library, "get", return_value=self.sequence):
            backend._sync_state()
            backend._load_black.assert_called_once()
            backend._sync_images.assert_not_called()
            self.controller.hardware.position_pct = (1 + (app.EXTEND_SEC - 1) / 2) * 100 / app.EXTEND_SEC
            backend._sync_state()
            self.assertAlmostEqual(backend._sync_images.call_args.args[2], 0.5)
            self.sequence.kind = "video"
            backend._sync_state()
            self.assertAlmostEqual(backend._sync_video.call_args.args[2], 0.5)

    def test_image_and_video_playback_fit_remaining_upstroke_at_any_speed(self):
        state = self.start_up()
        backend = app.MpvDisplayBackend(self.controller)
        backend._load_source = Mock(return_value=True)
        backend._duration_for_loaded_video = Mock(return_value=10.0)
        backend._set_speed = Mock()
        backend._set_pause = Mock()
        backend._seek = Mock()
        for duty in (1.0, 0.5):
            state["speed_duty"] = duty
            duration = (app.EXTEND_SEC - 1.0) / duty
            plan = backend._image_plan(self.sequence, state, 1)
            self.assertAlmostEqual(plan["duration_sec"], duration)
            backend._sync_video(self.sequence, state, 0.0, 1, True)
            backend._set_speed.assert_called_with(10.0 / duration)
            self.assertAlmostEqual(app.sequence_travel_duration_sec(self.sequence, -1, duty, 1.0), app.RETRACT_SEC / duty)


if __name__ == "__main__":
    unittest.main()
