"""Unit tests for maintainer dual-quality cache-file parsing and move_segment.

Mirrors the import-mocking pattern in ``test_maintainer.py`` so the heavy comms
modules don't need to be present at import time.
"""

import datetime
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_MOCKED_MODULES = [
    "frigate.comms.inter_process",
    "frigate.comms.detections_updater",
    "frigate.comms.recordings_updater",
    "frigate.config.camera.updater",
]
_originals = {name: sys.modules.get(name) for name in _MOCKED_MODULES}
for name in _MOCKED_MODULES:
    sys.modules[name] = MagicMock()

from frigate.config import FrigateConfig, RetainModeEnum  # noqa: E402
from frigate.models import Recordings  # noqa: E402
from frigate.record.maintainer import RecordingMaintainer, SegmentInfo  # noqa: E402

for name, orig in _originals.items():
    if orig is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = orig


def _make_maintainer():
    config = MagicMock(spec=FrigateConfig)
    config.cameras = {}
    stop_event = MagicMock()
    maintainer = RecordingMaintainer(config, stop_event)
    maintainer.end_time_cache = {}
    maintainer.unexpected_cache_files_logged = False
    return maintainer


class TestMoveFilesParsing(unittest.IsolatedAsyncioTestCase):
    async def _run_move_files(self, files, captured):
        maintainer = _make_maintainer()

        async def fake_validate(camera, reviews, recording):
            captured.append(recording)
            return None

        maintainer.validate_and_move_segment = fake_validate
        # no reviews path is exercised; cameras dict is empty so any camera match
        # would bail early. To let the recording reach our capture we set a
        # matching camera.
        cam_cfg = MagicMock()
        cam_cfg.record.enabled = True
        # Build a minimal cameras dict for the iteration at the end of move_files.
        maintainer.config.cameras = {"back": cam_cfg}

        # mock all the DB-touching and publisher bits
        maintainer.recordings_publisher = MagicMock()
        maintainer.requestor = MagicMock()

        with (
            patch("frigate.record.maintainer.os.listdir", return_value=files),
            patch("frigate.record.maintainer.os.path.isfile", return_value=True),
            patch("frigate.record.maintainer.psutil.process_iter", return_value=[]),
            patch("frigate.record.maintainer.ReviewSegment") as mock_rs,
        ):
            # avoid DB: make the reviews query iterate as empty list
            mock_rs.select.return_value.where.return_value.order_by.return_value = []
            await maintainer.move_files()

    async def test_move_files_parses_main_stream_filename(self):
        captured = []
        await self._run_move_files(["back@main@20260101000000+0000.mp4"], captured)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["stream_quality"], "main")

    async def test_move_files_parses_substream_filename_default_sub(self):
        captured = []
        await self._run_move_files(["back@20260101000000+0000.mp4"], captured)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["stream_quality"], "sub")

    async def test_move_files_camera_name_with_at_sign(self):
        """Camera names may legitimately contain '@'. The parser must
        rsplit on the rightmost '@<timestamp>' rather than naively
        splitting on every '@', so a file like
        ``back@foo@bar@<ts>.mp4`` is parsed as camera ``back@foo@bar``,
        quality ``sub`` (M5 fix)."""
        captured = []
        await self._run_move_files(
            ["back@foo@bar@20260101000000+0000.mp4"],
            captured,
        )
        # Camera was recognised under the at-bearing name.
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["stream_quality"], "sub")

    async def test_move_files_parses_main_collision_suffix(self):
        """``event_recorder`` appends ``_<counter>`` to the timestamp when
        two segments would otherwise collide on the same second. The
        maintainer parser must strip that suffix before strptime so the
        segment is still processed rather than silently dropped with an
        "unexpected files in cache" warning."""
        captured = []
        await self._run_move_files(
            ["back@main@20260101000000+0000_1.mp4"],
            captured,
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["stream_quality"], "main")

    async def test_move_files_camera_name_with_at_sign_main(self):
        """Same as above but the file is a main-stream segment
        (``<camera>@main@<ts>.mp4``). Camera name is ``back@foo``."""
        captured = []
        await self._run_move_files(
            ["back@foo@main@20260101000000+0000.mp4"],
            captured,
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["stream_quality"], "main")


class TestValidateAndMoveSegmentMainBranch(unittest.IsolatedAsyncioTestCase):
    async def test_validate_and_move_segment_main_always_kept(self):
        """stream_quality=main short-circuits through the main branch and calls
        move_segment with stream_quality="main" regardless of continuous/motion
        config when the retain mode is ``all``."""
        maintainer = _make_maintainer()

        cam_cfg = MagicMock()
        cam_cfg.record.enabled = True
        cam_cfg.record.continuous.days = 0
        cam_cfg.record.motion.days = 0
        cam_cfg.record.event_recording.retain.mode = RetainModeEnum.all
        maintainer.config.cameras = {"back": cam_cfg}

        maintainer.recordings_publisher = MagicMock()
        maintainer.move_segment = AsyncMock(return_value=None)
        # No detect frames recorded for this camera; with mode=all the
        # segment is retained anyway.
        maintainer.object_recordings_info["back"] = []
        maintainer.audio_recordings_info["back"] = []

        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        cache_path = "/tmp/back@main@20260101000000+0000.mp4"
        # prime end_time_cache so we don't probe with ffprobe
        maintainer.end_time_cache[cache_path] = (
            start + datetime.timedelta(seconds=10),
            10.0,
        )

        recording = {
            "cache_path": cache_path,
            "start_time": start,
            "stream_quality": "main",
        }

        await maintainer.validate_and_move_segment("back", [], recording)

        self.assertEqual(maintainer.move_segment.call_count, 1)
        args, kwargs = maintainer.move_segment.call_args
        # move_segment(camera, start_time, end_time, duration, cache_path,
        # segment_info, stream_quality)
        self.assertEqual(args[0], "back")
        self.assertIsInstance(args[5], SegmentInfo)
        self.assertEqual(args[6], "main")


class TestMainBranchRetainModeSemantics(unittest.IsolatedAsyncioTestCase):
    """Pinning tests for the main-stream retention semantic.

    The maintainer's main-stream branch gates segments against the
    substream-derived detect stats (``self.object_recordings_info``). This is
    deliberate: operators who opt into ``mode=motion`` or ``active_objects``
    are accepting that gating; the default ``mode=all`` bypasses it. These
    tests pin the behaviour so a future refactor surfaces any change
    intentionally."""

    async def test_main_segment_dropped_when_motion_mode_and_no_substream_motion(
        self,
    ):
        """When the operator opts into mode=motion for event_recording.retain,
        main segments are gated against substream-derived motion stats.
        Pinning this behaviour so any future refactor surfaces it
        intentionally."""
        maintainer = _make_maintainer()

        cam_cfg = MagicMock()
        cam_cfg.record.enabled = True
        cam_cfg.record.continuous.days = 0
        cam_cfg.record.motion.days = 0
        cam_cfg.record.event_recording.retain.mode = RetainModeEnum.motion
        maintainer.config.cameras = {"back": cam_cfg}

        maintainer.recordings_publisher = MagicMock()
        maintainer.move_segment = AsyncMock(return_value=None)
        maintainer.drop_segment = MagicMock()
        # No substream detect frames -> motion_count == 0 -> with mode=motion
        # should_discard_segment returns True -> the segment is dropped.
        maintainer.object_recordings_info["back"] = []
        maintainer.audio_recordings_info["back"] = []

        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        cache_path = "/tmp/back@main@20260101000000+0000.mp4"
        maintainer.end_time_cache[cache_path] = (
            start + datetime.timedelta(seconds=10),
            10.0,
        )

        recording = {
            "cache_path": cache_path,
            "start_time": start,
            "stream_quality": "main",
        }

        result = await maintainer.validate_and_move_segment("back", [], recording)

        self.assertIsNone(result)
        maintainer.move_segment.assert_not_called()
        maintainer.drop_segment.assert_called_once_with(cache_path)

    async def test_main_segment_retained_when_retain_mode_is_all_by_default(self):
        """With the new ``RetainModeEnum.all`` default for
        event_recording.retain.mode, a main segment with zero substream motion
        stats IS retained. This documents the intent of the default change:
        the EventRecorder already filtered what to capture, so the maintainer
        should not re-gate on substream stats unless the operator opts in."""
        maintainer = _make_maintainer()

        cam_cfg = MagicMock()
        cam_cfg.record.enabled = True
        cam_cfg.record.continuous.days = 0
        cam_cfg.record.motion.days = 0
        cam_cfg.record.event_recording.retain.mode = RetainModeEnum.all
        maintainer.config.cameras = {"back": cam_cfg}

        maintainer.recordings_publisher = MagicMock()
        maintainer.move_segment = AsyncMock(return_value=None)
        maintainer.drop_segment = MagicMock()
        maintainer.object_recordings_info["back"] = []
        maintainer.audio_recordings_info["back"] = []

        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        cache_path = "/tmp/back@main@20260101000000+0000.mp4"
        maintainer.end_time_cache[cache_path] = (
            start + datetime.timedelta(seconds=10),
            10.0,
        )

        recording = {
            "cache_path": cache_path,
            "start_time": start,
            "stream_quality": "main",
        }

        await maintainer.validate_and_move_segment("back", [], recording)

        maintainer.move_segment.assert_called_once()
        maintainer.drop_segment.assert_not_called()
        args, _ = maintainer.move_segment.call_args
        self.assertIsInstance(args[5], SegmentInfo)
        self.assertEqual(args[5].motion_count, 0)
        self.assertEqual(args[6], "main")


class TestMoveSegmentFilenameAndRecord(unittest.IsolatedAsyncioTestCase):
    async def _invoke_move_segment(self, stream_quality, tmpdir):
        maintainer = _make_maintainer()
        cam_cfg = MagicMock()
        cam_cfg.record.enabled = True
        maintainer.config.cameras = {"back": cam_cfg}
        maintainer.config.ffmpeg.ffmpeg_path = "/bin/true"
        maintainer.recordings_publisher = MagicMock()

        # Build a real SegmentInfo to pass through move_segment. The fields
        # here mirror what production code passes: zero counts, no heatmap.
        segment_info = SegmentInfo(
            motion_count=0,
            active_object_count=0,
            region_count=0,
            average_dBFS=0,
            motion_heatmap=None,
        )
        maintainer.segment_stats = MagicMock(return_value=segment_info)

        start = datetime.datetime(2026, 1, 1, 12, 34, 56, tzinfo=datetime.timezone.utc)
        end = start + datetime.timedelta(seconds=10)
        cache_path = os.path.join(tmpdir, "cache_input.mp4")
        # create the source file so getsize works
        with open(cache_path, "wb") as f:
            f.write(b"\x00" * 1024)

        captured_paths = []

        async def fake_create_subprocess_exec(*args, **kwargs):
            # last positional-arg is the output file path (per move_segment)
            captured_paths.append(args[-1])
            # create the output file so the os.path.exists check on next run
            # would be True, and getsize works
            with open(args[-1], "wb") as f:
                f.write(b"\x00" * 1024)
            p = MagicMock()
            p.returncode = 0
            p.wait = AsyncMock(return_value=0)
            p.stderr = None
            return p

        with (
            patch(
                "frigate.record.maintainer.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ),
            patch("frigate.record.maintainer.RECORD_DIR", tmpdir),
            patch("frigate.record.maintainer.os.remove"),
        ):
            result = await maintainer.move_segment(
                "back",
                start,
                end,
                10.0,
                cache_path,
                segment_info,
                stream_quality,
            )

        return result, captured_paths

    async def test_move_segment_writes_main_suffix_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            result, paths = await self._invoke_move_segment("main", tmpdir)

        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith("34.56_main.mp4"), paths[0])

    async def test_move_segment_sub_has_no_suffix(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            result, paths = await self._invoke_move_segment("sub", tmpdir)

        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].endswith("34.56.mp4"), paths[0])
        self.assertNotIn("_sub", paths[0])

    async def test_move_segment_returns_stream_quality_in_record(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            result, _ = await self._invoke_move_segment("main", tmpdir)

        self.assertIsNotNone(result)
        self.assertEqual(result[Recordings.stream_quality.name], "main")


class TestMainBasenameProducesMainRecord(unittest.IsolatedAsyncioTestCase):
    """End-to-end parse pipeline: a ``@main@`` cache filename must result in
    a ``move_segment`` invocation (and therefore a Recordings insert payload)
    carrying ``stream_quality="main"``, not the CharField default of "sub".

    Regression test for the bug where event_recorder promoted
    ``camera@main@ts.mp4`` files but Recordings rows were tagged sub, which
    broke vod_ts main/sub merge logic and produced overlapping playlist
    intervals."""

    async def test_main_basename_propagates_to_move_segment(self):
        import tempfile

        maintainer = _make_maintainer()

        cam_cfg = MagicMock()
        cam_cfg.record.enabled = True
        cam_cfg.record.continuous.days = 0
        cam_cfg.record.motion.days = 0
        cam_cfg.record.event_recording.retain.mode = RetainModeEnum.all
        maintainer.config.cameras = {"back": cam_cfg}
        maintainer.recordings_publisher = MagicMock()
        maintainer.requestor = MagicMock()
        maintainer.move_segment = AsyncMock(
            return_value={
                Recordings.stream_quality.name: "main",
                Recordings.path.name: "/tmp/00.00_main.mp4",
            }
        )

        # prime end_time_cache so validate_and_move_segment doesn't ffprobe
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = "back@main@20260101000000+0000.mp4"
            cache_path = os.path.join(tmpdir, cache_file)
            with open(cache_path, "wb") as f:
                f.write(b"\x00" * 1024)
            start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
            maintainer.end_time_cache[cache_path] = (
                start + datetime.timedelta(seconds=10),
                10.0,
            )

            with (
                patch("frigate.record.maintainer.CACHE_DIR", tmpdir),
                patch(
                    "frigate.record.maintainer.os.listdir",
                    return_value=[cache_file],
                ),
                patch("frigate.record.maintainer.os.path.isfile", return_value=True),
                patch("frigate.record.maintainer.psutil.process_iter", return_value=[]),
                patch("frigate.record.maintainer.ReviewSegment") as mock_rs,
            ):
                mock_rs.select.return_value.where.return_value.order_by.return_value = []
                await maintainer.move_files()

        self.assertEqual(maintainer.move_segment.call_count, 1)
        args, _ = maintainer.move_segment.call_args
        # signature: camera, start_time, end_time, duration, cache_path,
        # segment_info, stream_quality
        self.assertEqual(args[6], "main")


class TestRecordingRowIsMainFallback(unittest.TestCase):
    """Reader-side defensive fallback: ``recording_row_is_main`` must detect
    main-quality rows even when ``stream_quality`` is mis-tagged as "sub",
    by inspecting the ``_main.mp4`` path suffix. This provides backward
    compatibility for rows inserted before the maintainer was corrected."""

    def _row(self, stream_quality, path):
        r = MagicMock()
        r.stream_quality = stream_quality
        r.path = path
        return r

    def test_detects_main_via_stream_quality_column(self):
        from frigate.api.media import recording_row_is_main

        self.assertTrue(
            recording_row_is_main(
                self._row("main", "/media/frigate/recordings/back/00.00.mp4")
            )
        )

    def test_detects_main_via_path_suffix_when_column_wrong(self):
        from frigate.api.media import recording_row_is_main

        # Row that an older-revision maintainer would have inserted: the
        # file is clearly main (the ``_main.mp4`` suffix comes from
        # move_segment) but the column got the default "sub".
        self.assertTrue(
            recording_row_is_main(
                self._row(
                    "sub",
                    "/media/frigate/recordings/2026-01-01/12/back/34.56_main.mp4",
                )
            )
        )

    def test_sub_row_without_suffix_is_not_main(self):
        from frigate.api.media import recording_row_is_main

        self.assertFalse(
            recording_row_is_main(
                self._row(
                    "sub",
                    "/media/frigate/recordings/2026-01-01/12/back/34.56.mp4",
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
