"""Unit tests for the ring-buffer-promote `EventRecorder`.

The post-rewrite `EventRecorder` no longer spawns ffmpeg. It watches a
per-camera buffer directory that the camera ffmpeg (role ``record_events``)
fills continuously, prunes old files when idle, and promotes buffered
segments into ``CACHE_DIR`` as ``camera@main@<ts>.mp4`` on trigger.

These tests exercise the real `_tick_buffers` / `_promote_segment` code
paths against a real tmpfs-like ``tempfile.TemporaryDirectory`` with
files whose mtimes we control explicitly.

Style mirrors ``frigate/test/test_maintainer.py``: mock the noisy comms
modules at import time, then drive the class under test directly.
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

# Mock complex comms imports before importing the module under test.
_MOCKED_MODULES = [
    "frigate.comms.inter_process",
    "frigate.comms.detections_updater",
]
_originals = {name: sys.modules.get(name) for name in _MOCKED_MODULES}
for name in _MOCKED_MODULES:
    sys.modules[name] = MagicMock()

from frigate.const import CACHE_SEGMENT_FORMAT  # noqa: E402
from frigate.record import event_recorder as ev  # noqa: E402

for name, orig in _originals.items():
    if orig is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = orig


def _ts_filename(dt: datetime.datetime) -> str:
    """Produce a filename matching the CACHE_SEGMENT_FORMAT pattern."""
    return dt.strftime(CACHE_SEGMENT_FORMAT) + ".mp4"


def _make_camera_cfg(
    pre_capture: int = 5,
    post_capture: int = 10,
    event_recording_enabled: bool = True,
):
    cfg = MagicMock()
    cfg.record.event_recording.pre_capture = pre_capture
    cfg.record.event_recording.post_capture = post_capture
    cfg.record.event_recording.enabled = event_recording_enabled
    return cfg


class _RecorderFixture(unittest.TestCase):
    """Shared setup: tmp CACHE_DIR + tmp event buffer dir, and an
    EventRecorder instance with the mocked DetectionSubscriber."""

    def setUp(self) -> None:
        self._tmp_cache = tempfile.TemporaryDirectory()
        self._tmp_buffer = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_cache.cleanup)
        self.addCleanup(self._tmp_buffer.cleanup)

        self.cache_dir = self._tmp_cache.name
        self.buffer_base = self._tmp_buffer.name

        # Patch module-level CACHE_DIR / EVENT_BUFFER_BASE_DIR so the
        # recorder reads from our temporary directories.
        self._patches = [
            patch.object(ev, "CACHE_DIR", self.cache_dir),
            patch.object(ev, "EVENT_BUFFER_BASE_DIR", self.buffer_base),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _make_recorder(
        self,
        cameras=("cam1",),
        pre_capture: int = 5,
        post_capture: int = 10,
        event_recording_enabled: bool = True,
    ) -> ev.EventRecorder:
        frigate_config = MagicMock()
        camera_configs = {
            c: _make_camera_cfg(
                pre_capture=pre_capture,
                post_capture=post_capture,
                event_recording_enabled=event_recording_enabled,
            )
            for c in cameras
        }
        stop_event = MagicMock()
        stop_event.is_set.return_value = False

        with patch.object(ev, "DetectionSubscriber") as sub_cls:
            sub_cls.return_value = MagicMock()
            recorder = ev.EventRecorder(
                frigate_config, camera_configs, {}, stop_event
            )
        return recorder

    def _seed_buffer_file(
        self,
        camera: str,
        when: datetime.datetime,
        mtime_override: float | None = None,
    ) -> str:
        """Create a buffer file whose *name* is a valid strftime timestamp
        and whose mtime is (optionally) different — this lets us test that
        the promoted ts comes from the filename not the mtime (M8)."""
        cam_dir = os.path.join(self.buffer_base, camera)
        os.makedirs(cam_dir, exist_ok=True)
        path = os.path.join(cam_dir, _ts_filename(when))
        with open(path, "wb") as fp:
            fp.write(b"x")
        m = mtime_override if mtime_override is not None else when.timestamp()
        os.utime(path, (m, m))
        return path


class TestIdlePrune(_RecorderFixture):
    def test_prunes_files_older_than_pre_capture_keeps_newest(self):
        recorder = self._make_recorder(cameras=("cam1",), pre_capture=5)

        now = time.time()
        # Three closed segments: two older than pre_capture, one "new".
        old_dt = datetime.datetime.fromtimestamp(now - 60)
        mid_dt = datetime.datetime.fromtimestamp(now - 30)
        recent_dt = datetime.datetime.fromtimestamp(now - 1)

        old = self._seed_buffer_file("cam1", old_dt)
        mid = self._seed_buffer_file("cam1", mid_dt)
        recent = self._seed_buffer_file("cam1", recent_dt)

        recorder._tick_buffers()

        # Old files pruned; newest always retained.
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(mid))
        self.assertTrue(os.path.exists(recent))

    def test_idle_retains_newest_even_if_older_than_pre_capture(self):
        """On very low-fps streams the only file in the buffer can legit
        be older than the cutoff; we must never empty the buffer so a
        later trigger still has a pre-roll segment."""
        recorder = self._make_recorder(cameras=("cam1",), pre_capture=5)
        long_ago = datetime.datetime.fromtimestamp(time.time() - 3600)
        path = self._seed_buffer_file("cam1", long_ago)

        recorder._tick_buffers()

        self.assertTrue(os.path.exists(path))


class TestTriggerPromotion(_RecorderFixture):
    def test_trigger_promotes_buffered_segments(self):
        recorder = self._make_recorder(
            cameras=("cam1",), pre_capture=10, post_capture=5
        )
        now = time.time()

        dt1 = datetime.datetime.fromtimestamp(now - 8)
        dt2 = datetime.datetime.fromtimestamp(now - 4)
        # Newest file — still being written — stays in the buffer.
        dt3 = datetime.datetime.fromtimestamp(now - 1)
        self._seed_buffer_file("cam1", dt1)
        self._seed_buffer_file("cam1", dt2)
        newest = self._seed_buffer_file("cam1", dt3)

        # Flip to active.
        state = recorder.camera_states["cam1"]
        state.is_active = True
        state.last_activity_time = now

        recorder._tick_buffers()

        # Two older files promoted into CACHE_DIR with @main@ naming; the
        # newest (still being written) must stay in the buffer.
        promoted = [
            f for f in os.listdir(self.cache_dir)
            if f.startswith("cam1@main@")
        ]
        self.assertEqual(len(promoted), 2)
        self.assertTrue(os.path.exists(newest))

    def test_continues_promoting_during_post_capture_window(self):
        """After a trigger, new segments that arrive within post_capture
        seconds of the last activity should continue to be promoted."""
        recorder = self._make_recorder(
            cameras=("cam1",), pre_capture=2, post_capture=30
        )
        now = time.time()

        state = recorder.camera_states["cam1"]
        state.is_active = True
        state.last_activity_time = now  # still inside post window

        # One "ongoing" segment + a newest file still open.
        dt_closed = datetime.datetime.fromtimestamp(now - 2)
        dt_open = datetime.datetime.fromtimestamp(now - 0.1)
        self._seed_buffer_file("cam1", dt_closed)
        self._seed_buffer_file("cam1", dt_open)

        recorder._tick_buffers()

        promoted = [
            f for f in os.listdir(self.cache_dir)
            if f.startswith("cam1@main@")
        ]
        self.assertEqual(len(promoted), 1)

    def test_returns_to_idle_after_post_capture_elapsed(self):
        recorder = self._make_recorder(
            cameras=("cam1",), pre_capture=2, post_capture=5
        )
        now = time.time()
        state = recorder.camera_states["cam1"]
        state.is_active = True
        state.last_activity_time = now - 20  # elapsed
        state.promoted_paths = {"/some/path"}

        # Seed a couple files so the idle branch has something to work on.
        self._seed_buffer_file(
            "cam1", datetime.datetime.fromtimestamp(now - 3)
        )
        self._seed_buffer_file(
            "cam1", datetime.datetime.fromtimestamp(now - 1)
        )

        recorder._tick_buffers()

        self.assertFalse(state.is_active)
        self.assertEqual(state.promoted_paths, set())


class TestNoEventRecordingCamera(_RecorderFixture):
    def test_no_event_recording_enabled_tick_is_noop(self):
        """If no camera has event_recording.enabled, _tick_buffers on an
        empty buffer directory must not raise and must not write into
        CACHE_DIR."""
        recorder = self._make_recorder(
            cameras=("cam1",), event_recording_enabled=False
        )
        # Nothing seeded -> nothing to promote or prune.
        recorder._tick_buffers()
        self.assertEqual(os.listdir(self.cache_dir), [])


class TestCameraWithAtInName(_RecorderFixture):
    def test_promotion_filename_uses_at_main_suffix(self):
        """A camera named e.g. ``back@dvr`` (legal per the current regex
        for DVR-style multi-input setups in some forks) still promotes
        to ``<camera>@main@<ts>.mp4``."""
        camera = "cam_multi"  # use a regex-valid name
        recorder = self._make_recorder(
            cameras=(camera,), pre_capture=10, post_capture=30
        )
        now = time.time()

        state = recorder.camera_states[camera]
        state.is_active = True
        state.last_activity_time = now

        dt_closed = datetime.datetime.fromtimestamp(now - 3)
        dt_open = datetime.datetime.fromtimestamp(now - 0.1)
        self._seed_buffer_file(camera, dt_closed)
        self._seed_buffer_file(camera, dt_open)

        recorder._tick_buffers()

        promoted = sorted(os.listdir(self.cache_dir))
        self.assertEqual(len(promoted), 1)
        self.assertTrue(promoted[0].startswith(f"{camera}@main@"))
        self.assertTrue(promoted[0].endswith(".mp4"))


class TestFilenameTimestampRoundtrip(_RecorderFixture):
    def test_promoted_timestamp_comes_from_filename_not_mtime(self):
        """M8: the promoted filename timestamp must match the *buffer
        filename* timestamp, not the file's mtime. Drifted mtimes
        (segment write-completion time) would otherwise shift main
        intervals forward relative to sub and cause vod_ts to fall back
        to SD at the start of every event."""
        recorder = self._make_recorder(
            cameras=("cam1",), pre_capture=10, post_capture=30
        )
        now = time.time()
        state = recorder.camera_states["cam1"]
        state.is_active = True
        state.last_activity_time = now

        # Name-timestamp is T-5, but mtime is skewed forward by ~3 s to
        # simulate write-completion-time drift.
        name_dt = datetime.datetime.fromtimestamp(now - 5)
        self._seed_buffer_file(
            "cam1", name_dt, mtime_override=now - 2
        )
        # Newest sentinel so the older one is considered closed.
        self._seed_buffer_file(
            "cam1", datetime.datetime.fromtimestamp(now - 0.1)
        )

        recorder._tick_buffers()

        promoted = [
            f for f in os.listdir(self.cache_dir)
            if f.startswith("cam1@main@")
        ]
        self.assertEqual(len(promoted), 1)
        # Expected suffix mirrors the name_dt in CACHE_SEGMENT_FORMAT.
        expected_ts = name_dt.strftime(CACHE_SEGMENT_FORMAT)
        self.assertIn(expected_ts, promoted[0])

    def test_promoted_filename_parses_back_to_original_datetime(self):
        """Round-trip: parse the promoted ts back through strptime and
        confirm it equals the seeded buffer filename's datetime."""
        recorder = self._make_recorder(
            cameras=("cam1",), pre_capture=10, post_capture=30
        )
        now = time.time()
        state = recorder.camera_states["cam1"]
        state.is_active = True
        state.last_activity_time = now

        name_dt = datetime.datetime.fromtimestamp(now - 5)
        self._seed_buffer_file("cam1", name_dt)
        self._seed_buffer_file(
            "cam1", datetime.datetime.fromtimestamp(now - 0.1)
        )

        recorder._tick_buffers()

        promoted = [
            f for f in os.listdir(self.cache_dir)
            if f.startswith("cam1@main@")
        ]
        self.assertEqual(len(promoted), 1)
        # "cam1@main@<ts>.mp4" -> extract <ts>
        ts_part = promoted[0].split("@main@", 1)[1]
        ts_part = ts_part[: -len(".mp4")]
        parsed = datetime.datetime.strptime(ts_part, CACHE_SEGMENT_FORMAT)
        # strftime → strptime round-trip drops sub-second resolution.
        expected = datetime.datetime.strptime(
            name_dt.strftime(CACHE_SEGMENT_FORMAT), CACHE_SEGMENT_FORMAT
        )
        self.assertEqual(parsed, expected)


if __name__ == "__main__":
    unittest.main()
