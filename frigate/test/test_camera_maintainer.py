"""Tests for CameraMaintainer SHM cleanup on camera remove.

Regression coverage for the case where a camera is removed and then a
new camera is added with the same name. Without unlinking the per-frame
YUV SHM slots, the maintainer's frame_manager.create call hits
FileExistsError and falls back to reopening the existing segment at the
*old* size, which the new ffmpeg process then writes mismatched-size
frames into.
"""

import unittest
from unittest.mock import MagicMock, patch

from frigate.camera.maintainer import CameraMaintainer


class TestMaintainerUnlinkFrameSlotsOnRemove(unittest.TestCase):
    def _make_maintainer(self) -> CameraMaintainer:
        """Build a maintainer without invoking __init__ (avoids needing real
        FrigateConfig, queues, multiprocessing manager, etc.). We're only
        exercising the SHM-cleanup helper, so the surrounding init is
        irrelevant."""
        maintainer = CameraMaintainer.__new__(CameraMaintainer)
        maintainer.frame_manager = MagicMock()
        return maintainer

    def test_unlinks_only_segments_with_matching_prefix(self) -> None:
        maintainer = self._make_maintainer()
        maintainer.frame_manager.shm_store = {
            "front_frame0": object(),
            "front_frame1": object(),
            "front_frame2": object(),
            # Different camera; must not be touched.
            "side_frame0": object(),
            # Detector input/output buffers are sized by the model and
            # cached by the long-lived DetectorRunner — must not be
            # touched even when their owning camera is removed.
            "front": object(),
            "out-front": object(),
        }

        # __name-mangled access from outside the class.
        maintainer._CameraMaintainer__unlink_camera_frame_slots("front")

        deleted = [c.args[0] for c in maintainer.frame_manager.delete.call_args_list]
        self.assertEqual(
            sorted(deleted),
            ["front_frame0", "front_frame1", "front_frame2"],
        )

    def test_handles_camera_with_no_slots(self) -> None:
        """Cameras that were removed before any frame slot was ever
        created (e.g. cancelled during preparing_clip) should be a no-op."""
        maintainer = self._make_maintainer()
        maintainer.frame_manager.shm_store = {"other_frame0": object()}

        maintainer._CameraMaintainer__unlink_camera_frame_slots("front")

        maintainer.frame_manager.delete.assert_not_called()

    def test_swallows_delete_errors(self) -> None:
        """Unlink failures shouldn't abort the remove loop — best-effort."""
        maintainer = self._make_maintainer()
        maintainer.frame_manager.shm_store = {
            "front_frame0": object(),
            "front_frame1": object(),
        }
        maintainer.frame_manager.delete.side_effect = OSError("simulated")

        # Both slots are attempted; the OSError on the first doesn't
        # prevent the second from being tried.
        with patch("frigate.camera.maintainer.logger"):
            maintainer._CameraMaintainer__unlink_camera_frame_slots("front")

        self.assertEqual(maintainer.frame_manager.delete.call_count, 2)


class TestMaintainerCameraRecovery(unittest.TestCase):
    def test_recovery_preserves_metrics_and_frame_count(self) -> None:
        maintainer = CameraMaintainer.__new__(CameraMaintainer)
        camera_config = MagicMock(enabled_in_config=True, enabled=True)
        maintainer.config = MagicMock(cameras={"Front_Gate": camera_config})
        metrics = object()
        ptz_metrics = object()
        maintainer.camera_metrics = {"Front_Gate": metrics}
        maintainer.ptz_metrics = {"Front_Gate": ptz_metrics}
        maintainer.shm_count = 40
        capture = MagicMock(shm_frame_count=23)
        maintainer.capture_processes = {"Front_Gate": capture}
        maintainer.camera_processes = {"Front_Gate": MagicMock()}

        with (
            patch.object(
                maintainer,
                "_CameraMaintainer__stop_camera_capture_process",
            ) as stop_capture,
            patch.object(
                maintainer,
                "_CameraMaintainer__stop_camera_process",
            ) as stop_tracker,
            patch.object(
                maintainer,
                "_CameraMaintainer__unlink_camera_frame_slots",
            ) as unlink_slots,
            patch.object(
                maintainer,
                "_CameraMaintainer__start_camera_processor",
            ) as start_tracker,
            patch.object(
                maintainer,
                "_CameraMaintainer__start_camera_capture",
            ) as start_capture,
        ):
            maintainer._CameraMaintainer__restart_camera_processes("Front_Gate")

        stop_capture.assert_called_once_with("Front_Gate")
        stop_tracker.assert_called_once_with("Front_Gate")
        unlink_slots.assert_called_once_with("Front_Gate")
        start_tracker.assert_called_once_with("Front_Gate", camera_config)
        start_capture.assert_called_once_with(
            "Front_Gate",
            camera_config,
            shm_frame_count=23,
        )
        self.assertIs(metrics, maintainer.camera_metrics["Front_Gate"])
        self.assertIs(ptz_metrics, maintainer.ptz_metrics["Front_Gate"])

    def test_disabled_camera_is_not_supervised(self) -> None:
        maintainer = CameraMaintainer.__new__(CameraMaintainer)
        camera_config = MagicMock(enabled_in_config=True, enabled=False)
        maintainer.config = MagicMock(cameras={"Front_Gate": camera_config})
        maintainer.camera_processes = {"Front_Gate": MagicMock()}
        maintainer.capture_processes = {"Front_Gate": MagicMock()}
        maintainer.camera_metrics = {"Front_Gate": MagicMock()}
        maintainer.process_supervisor = MagicMock()

        maintainer._CameraMaintainer__check_camera_processes()

        maintainer.process_supervisor.forget.assert_called_once_with("Front_Gate")
        maintainer.process_supervisor.check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
