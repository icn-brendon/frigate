"""Tests for per-camera process failure and stall recovery."""

import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock

from frigate.camera.process_supervisor import CameraProcessSupervisor


@dataclass
class _Process:
    alive: bool
    pid: int
    exitcode: int | None

    def is_alive(self) -> bool:
        return self.alive


class TestCameraProcessSupervisor(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100.0
        self.restart_camera = MagicMock()
        self.supervisor = CameraProcessSupervisor(
            self.restart_camera,
            stale_after=60.0,
            clock=lambda: self.now,
        )
        self.tracker = _Process(alive=True, pid=4101, exitcode=None)
        self.capture = _Process(alive=True, pid=4102, exitcode=None)

    def test_dead_tracker_restarts_camera_and_logs_exit_reason(self) -> None:
        self.tracker.alive = False
        self.tracker.exitcode = -9

        with self.assertLogs(
            "frigate.camera.process_supervisor", level="WARNING"
        ) as logs:
            restarted = self.supervisor.check(
                "Front_Gate", self.tracker, self.capture, detection_frame=1000.0
            )

        self.assertTrue(restarted)
        self.restart_camera.assert_called_once_with("Front_Gate")
        self.assertIn("tracker pid=4101 exitcode=-9", "\n".join(logs.output))

    def test_initial_detection_frame_grace_does_not_restart(self) -> None:
        self.assertFalse(
            self.supervisor.check(
                "Front_Gate", self.tracker, self.capture, detection_frame=0.0
            )
        )
        self.now += 59.0
        self.assertFalse(
            self.supervisor.check(
                "Front_Gate", self.tracker, self.capture, detection_frame=0.0
            )
        )
        self.restart_camera.assert_not_called()

    def test_detection_frame_progress_keeps_tracker_healthy(self) -> None:
        self.assertFalse(
            self.supervisor.check(
                "Front_Gate", self.tracker, self.capture, detection_frame=1000.0
            )
        )
        self.now += 61.0
        self.assertFalse(
            self.supervisor.check(
                "Front_Gate", self.tracker, self.capture, detection_frame=1001.0
            )
        )
        self.restart_camera.assert_not_called()

    def test_alive_tracker_with_stale_detection_frame_restarts(self) -> None:
        self.assertFalse(
            self.supervisor.check(
                "Front_Gate", self.tracker, self.capture, detection_frame=1000.0
            )
        )
        self.now += 61.0

        with self.assertLogs(
            "frigate.camera.process_supervisor", level="WARNING"
        ) as logs:
            restarted = self.supervisor.check(
                "Front_Gate", self.tracker, self.capture, detection_frame=1000.0
            )

        self.assertTrue(restarted)
        self.restart_camera.assert_called_once_with("Front_Gate")
        self.assertIn("detection frame stalled", "\n".join(logs.output))

    def test_repeated_failures_back_off_then_retry(self) -> None:
        supervisor = CameraProcessSupervisor(
            self.restart_camera,
            max_restarts=2,
            restart_window=60.0,
            clock=lambda: self.now,
        )
        self.tracker.alive = False
        self.tracker.exitcode = 1

        self.assertTrue(
            supervisor.check(
                "Front_Gate", self.tracker, self.capture, detection_frame=1000.0
            )
        )
        self.assertTrue(
            supervisor.check(
                "Front_Gate", self.tracker, self.capture, detection_frame=1000.0
            )
        )
        with self.assertLogs(
            "frigate.camera.process_supervisor", level="ERROR"
        ) as logs:
            self.assertFalse(
                supervisor.check(
                    "Front_Gate",
                    self.tracker,
                    self.capture,
                    detection_frame=1000.0,
                )
            )

        self.assertEqual(self.restart_camera.call_count, 2)
        self.assertIn("backing off", "\n".join(logs.output))

        self.now += 61.0
        self.assertTrue(
            supervisor.check(
                "Front_Gate", self.tracker, self.capture, detection_frame=1000.0
            )
        )
        self.assertEqual(self.restart_camera.call_count, 3)


if __name__ == "__main__":
    unittest.main()
