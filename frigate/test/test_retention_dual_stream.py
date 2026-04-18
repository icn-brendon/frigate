"""Unit tests for retention behaviour in the presence of main-stream segments.

These tests exercise the two independent passes inside
``frigate.record.cleanup.RecordingCleanup``:

* ``expire_existing_camera_recordings``          -> substream / legacy rows
* ``expire_existing_camera_main_recordings``     -> main-stream event rows

The goal is to prove that *no un-needed data is retained* and no needed data is
accidentally deleted when both qualities coexist. Each test asserts on **both**
the DB row state and the on-disk file state.

Style: unittest + a real in-memory SQLite via peewee, with a
``tempfile.TemporaryDirectory`` for all on-disk recording paths. Never touches
the real ``/media/frigate`` or ``/tmp/cache``.
"""

import datetime
import os
import tempfile
import types
import unittest
from unittest.mock import MagicMock

from playhouse.sqlite_ext import SqliteExtDatabase

from frigate.config import RetainModeEnum
from frigate.models import Previews, Recordings, ReviewSegment, UserReviewStatus
from frigate.record.cleanup import RecordingCleanup

DAY = 24 * 60 * 60


def _make_camera_config(
    name: str = "back",
    event_retain_days: float = 7,
    event_retain_mode: RetainModeEnum = RetainModeEnum.all,
    continuous_days: float = 0,
    motion_days: float = 0,
    alerts_pre: int = 5,
    alerts_post: int = 5,
    detections_pre: int = 5,
    detections_post: int = 5,
    alerts_mode: RetainModeEnum = RetainModeEnum.motion,
    detections_mode: RetainModeEnum = RetainModeEnum.motion,
    event_retain_present: bool = True,
):
    """Construct a duck-typed CameraConfig.

    A full ``FrigateConfig`` is heavy; cleanup.py only reads a narrow slice of
    attributes, so a ``SimpleNamespace`` is sufficient and faster to build.
    """

    def _retain(days, mode=RetainModeEnum.all):
        return types.SimpleNamespace(days=days, mode=mode)

    event_recording = types.SimpleNamespace(
        retain=(
            _retain(event_retain_days, event_retain_mode)
            if event_retain_present
            else None
        ),
    )

    alerts = types.SimpleNamespace(
        pre_capture=alerts_pre,
        post_capture=alerts_post,
        retain=_retain(days=0, mode=alerts_mode),
    )
    detections = types.SimpleNamespace(
        pre_capture=detections_pre,
        post_capture=detections_post,
        retain=_retain(days=0, mode=detections_mode),
    )

    record = types.SimpleNamespace(
        event_recording=event_recording,
        continuous=_retain(continuous_days),
        motion=_retain(motion_days),
        alerts=alerts,
        detections=detections,
    )

    def _get_review_pre_capture(severity):
        return alerts.pre_capture if severity == "alert" else detections.pre_capture

    def _get_review_post_capture(severity):
        return alerts.post_capture if severity == "alert" else detections.post_capture

    record.get_review_pre_capture = _get_review_pre_capture
    record.get_review_post_capture = _get_review_post_capture

    return types.SimpleNamespace(name=name, record=record)


class _DualStreamRetentionBase(unittest.TestCase):
    """Shared fixture: in-memory SQLite, tmp recordings dir, cleanup instance."""

    def setUp(self):
        self.db = SqliteExtDatabase(":memory:")
        self._models = [Recordings, Previews, ReviewSegment, UserReviewStatus]
        self.db.bind(self._models)
        self.db.connect()
        self.db.create_tables(self._models)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.record_dir = self._tmp.name

        self.now = datetime.datetime(2026, 4, 15, 12, 0, 0)

        self.cleanup = RecordingCleanup.__new__(RecordingCleanup)
        self.cleanup.config = MagicMock()
        self.cleanup.stop_event = MagicMock()

    def tearDown(self):
        if not self.db.is_closed():
            self.db.close()

    # ---------------------------------------------------------------------
    # helpers
    # ---------------------------------------------------------------------
    def _seed(
        self,
        rec_id: str,
        camera: str,
        stream_quality: str,
        end_offset_seconds: float,
        *,
        motion: int = 0,
        objects: int = 0,
        dBFS: int = 0,
        duration: float = 10.0,
    ):
        """Insert a Recordings row and create the matching on-disk file.

        ``end_offset_seconds`` is subtracted from ``self.now`` — positive means
        older. The file path is ``<record_dir>/<camera>/<id>[_main].mp4`` where
        the ``_main`` suffix is appended for main-stream rows so that
        ``cleanup.expire_existing_camera_main_recordings`` recognises it as
        safe to unlink (its "defence in depth" check).
        """
        end_ts = self.now.timestamp() - end_offset_seconds
        start_ts = end_ts - duration
        cam_dir = os.path.join(self.record_dir, camera.replace("@", "_"))
        os.makedirs(cam_dir, exist_ok=True)
        suffix = "_main" if stream_quality == "main" else ""
        file_path = os.path.join(cam_dir, f"{rec_id}{suffix}.mp4")
        with open(file_path, "wb") as fp:
            fp.write(b"x")

        Recordings.insert(
            id=rec_id,
            camera=camera,
            path=file_path,
            start_time=start_ts,
            end_time=end_ts,
            duration=duration,
            motion=motion,
            objects=objects,
            dBFS=dBFS,
            segment_size=0.001,
            stream_quality=stream_quality,
        ).execute()
        return file_path

    def _exists(self, path: str) -> bool:
        return os.path.exists(path)

    def _row_exists(self, rec_id: str) -> bool:
        return Recordings.select().where(Recordings.id == rec_id).count() > 0


# =====================================================================
# tests 1 + 2: each pass touches only its own quality
# =====================================================================
class TestQualityIsolation(_DualStreamRetentionBase):
    def test_sub_retention_does_not_touch_main_rows_or_files(self):
        """expire_existing_camera_recordings (sub pass) must leave every main
        row and every ``_main.mp4`` file on disk alone even when the main row
        is ancient."""
        old = 30 * DAY
        sub_path = self._seed("sub_old", "back", "sub", old)
        main_path = self._seed("main_old", "back", "main", old)

        cfg = _make_camera_config(continuous_days=1, motion_days=1)
        continuous_expire = (self.now - datetime.timedelta(days=1)).timestamp()
        motion_expire = continuous_expire

        self.cleanup.expire_existing_camera_recordings(
            continuous_expire, motion_expire, cfg, reviews=[]
        )

        # sub row + file should be gone
        self.assertFalse(self._row_exists("sub_old"))
        self.assertFalse(self._exists(sub_path))
        # main row + file must be untouched
        self.assertTrue(self._row_exists("main_old"))
        self.assertTrue(self._exists(main_path))

    def test_main_retention_does_not_touch_sub_rows_or_files(self):
        """Symmetric of the above: main pass only affects rows where
        ``stream_quality == 'main'``."""
        old = 30 * DAY
        sub_path = self._seed("sub_old", "back", "sub", old)
        main_path = self._seed("main_old", "back", "main", old)

        cfg = _make_camera_config(event_retain_days=1)
        self.cleanup.expire_existing_camera_main_recordings(cfg, self.now)

        # main row + file should be gone
        self.assertFalse(self._row_exists("main_old"))
        self.assertFalse(self._exists(main_path))
        # sub row + file must be untouched
        self.assertTrue(self._row_exists("sub_old"))
        self.assertTrue(self._exists(sub_path))


# =====================================================================
# test 3: main expires on event_recording.retain.days
# =====================================================================
class TestMainRetainDays(_DualStreamRetentionBase):
    def test_main_expires_only_rows_older_than_retain_days(self):
        young_path = self._seed("main_young", "back", "main", 1 * DAY)
        old_path = self._seed("main_old", "back", "main", 30 * DAY)

        cfg = _make_camera_config(event_retain_days=7)
        self.cleanup.expire_existing_camera_main_recordings(cfg, self.now)

        self.assertTrue(self._row_exists("main_young"))
        self.assertTrue(self._exists(young_path))
        self.assertFalse(self._row_exists("main_old"))
        self.assertFalse(self._exists(old_path))


# =====================================================================
# tests 4 + 5: retain modes for main pass
# =====================================================================
class TestMainRetainModes(_DualStreamRetentionBase):
    def test_main_motion_mode_keeps_rows_with_motion(self):
        with_motion = self._seed("main_motion", "back", "main", 30 * DAY, motion=1)
        with_dbfs = self._seed("main_audio", "back", "main", 30 * DAY, dBFS=3)
        silent = self._seed("main_silent", "back", "main", 30 * DAY)

        cfg = _make_camera_config(
            event_retain_days=7, event_retain_mode=RetainModeEnum.motion
        )
        self.cleanup.expire_existing_camera_main_recordings(cfg, self.now)

        # motion + audio rows survive, silent row is purged
        self.assertTrue(self._row_exists("main_motion"))
        self.assertTrue(self._exists(with_motion))
        self.assertTrue(self._row_exists("main_audio"))
        self.assertTrue(self._exists(with_dbfs))
        self.assertFalse(self._row_exists("main_silent"))
        self.assertFalse(self._exists(silent))

    def test_main_motion_mode_keeps_recent_silent_rows(self):
        """Rows younger than the cutoff must not be considered at all."""
        young_silent = self._seed("main_young", "back", "main", 1 * DAY)

        cfg = _make_camera_config(
            event_retain_days=7, event_retain_mode=RetainModeEnum.motion
        )
        self.cleanup.expire_existing_camera_main_recordings(cfg, self.now)

        self.assertTrue(self._row_exists("main_young"))
        self.assertTrue(self._exists(young_silent))

    def test_main_active_objects_mode_only_keeps_rows_with_objects(self):
        with_obj = self._seed("main_obj", "back", "main", 30 * DAY, objects=1, motion=1)
        motion_only = self._seed("main_motion_only", "back", "main", 30 * DAY, motion=1)

        cfg = _make_camera_config(
            event_retain_days=7,
            event_retain_mode=RetainModeEnum.active_objects,
        )
        self.cleanup.expire_existing_camera_main_recordings(cfg, self.now)

        # only rows with tracked objects survive
        self.assertTrue(self._row_exists("main_obj"))
        self.assertTrue(self._exists(with_obj))
        self.assertFalse(self._row_exists("main_motion_only"))
        self.assertFalse(self._exists(motion_only))


# =====================================================================
# test 6: fallback to max(continuous, motion) when event_retain.days == 0
# =====================================================================
class TestMainFallback(_DualStreamRetentionBase):
    def test_fallback_uses_max_continuous_motion_when_event_retain_zero(self):
        """With event_recording.retain.days == 0, the main pass should use
        ``max(continuous.days, motion.days)`` (per cleanup.py B1 fix)."""
        boundary_keep = self._seed(
            "main_keep", "back", "main", 2 * DAY
        )  # younger than 3-day fallback
        boundary_drop = self._seed(
            "main_drop", "back", "main", 5 * DAY
        )  # older than 3-day fallback

        cfg = _make_camera_config(event_retain_days=0, continuous_days=3, motion_days=1)
        self.cleanup.expire_existing_camera_main_recordings(cfg, self.now)

        self.assertTrue(self._row_exists("main_keep"))
        self.assertTrue(self._exists(boundary_keep))
        self.assertFalse(self._row_exists("main_drop"))
        self.assertFalse(self._exists(boundary_drop))


# =====================================================================
# test 7: shorter event_recording retention than continuous ("ephemeral HD")
# =====================================================================
class TestEphemeralMainLongerSub(_DualStreamRetentionBase):
    def test_main_cleaned_while_sub_retained(self):
        """User wants HD main for a short window but keep sub long term."""
        # 5-day-old main should be deleted; 5-day-old sub should be retained.
        sub_path = self._seed("sub_5d", "back", "sub", 5 * DAY, motion=1)
        main_path = self._seed("main_5d", "back", "main", 5 * DAY, motion=1)

        cfg = _make_camera_config(
            event_retain_days=1, continuous_days=30, motion_days=30
        )
        # main pass: 1-day retention -> drops the 5-day-old main row
        self.cleanup.expire_existing_camera_main_recordings(cfg, self.now)
        # sub pass: 30-day retention -> keeps the 5-day-old sub row
        continuous_expire = (self.now - datetime.timedelta(days=30)).timestamp()
        self.cleanup.expire_existing_camera_recordings(
            continuous_expire, continuous_expire, cfg, reviews=[]
        )

        self.assertFalse(self._row_exists("main_5d"))
        self.assertFalse(self._exists(main_path))
        self.assertTrue(self._row_exists("sub_5d"))
        self.assertTrue(self._exists(sub_path))


# =====================================================================
# test 8: orphan file cleanup (EXPECTED FAILURE — no pass exists yet)
# =====================================================================
class TestOrphanFileSweep(_DualStreamRetentionBase):
    @unittest.expectedFailure
    def test_orphan_main_file_on_disk_is_swept(self):
        """A ``*_main.mp4`` file on disk with no matching Recordings row should
        be removed by the cleanup pass. Currently cleanup.py has no generic
        orphan-file sweep (only the ``no_camera_recordings`` DB pass, which
        works off rows, not files). This test is marked expectedFailure to
        document the gap — see TEST-PLAN-dual-stream.md follow-up.
        TODO: implement filesystem-side orphan sweep and flip to active.
        """
        # create a bare file with no DB row
        cam_dir = os.path.join(self.record_dir, "back")
        os.makedirs(cam_dir, exist_ok=True)
        orphan_path = os.path.join(cam_dir, "34.56_main.mp4")
        with open(orphan_path, "wb") as fp:
            fp.write(b"orphan")

        cfg = _make_camera_config(event_retain_days=7)
        self.cleanup.expire_existing_camera_main_recordings(cfg, self.now)

        self.assertFalse(
            self._exists(orphan_path),
            "orphan main file was not cleaned up",
        )


# =====================================================================
# test 9: camera name containing '@' still routes through stream_quality
# =====================================================================
class TestCameraNameWithAt(_DualStreamRetentionBase):
    def test_routing_uses_stream_quality_column_not_filename(self):
        """Cleanup routes by ``Recordings.stream_quality`` — not by filename —
        so a camera whose name contains ``@`` (exercising the M5 parser fix)
        has its main rows expired by the main pass and sub rows by the sub
        pass with no crossover."""
        old = 30 * DAY
        sub_path = self._seed("sub_old", "back@dvr", "sub", old)
        main_path = self._seed("main_old", "back@dvr", "main", old)

        cfg = _make_camera_config(
            name="back@dvr",
            event_retain_days=1,
            continuous_days=1,
            motion_days=1,
        )

        self.cleanup.expire_existing_camera_main_recordings(cfg, self.now)
        # main gone; sub still present
        self.assertFalse(self._row_exists("main_old"))
        self.assertFalse(self._exists(main_path))
        self.assertTrue(self._row_exists("sub_old"))
        self.assertTrue(self._exists(sub_path))

        continuous_expire = (self.now - datetime.timedelta(days=1)).timestamp()
        self.cleanup.expire_existing_camera_recordings(
            continuous_expire, continuous_expire, cfg, reviews=[]
        )
        self.assertFalse(self._row_exists("sub_old"))
        self.assertFalse(self._exists(sub_path))


# =====================================================================
# test 10: sequential sub+main passes on overlapping windows are safe
# =====================================================================
class TestConcurrentSweepSafety(_DualStreamRetentionBase):
    def test_sequential_passes_no_double_delete_no_orphan_rows(self):
        """Run the sub pass and the main pass back-to-back on overlapping
        time ranges. Neither pass should re-delete the other's row, and no DB
        row should remain whose file has been deleted."""
        old = 30 * DAY
        sub_path = self._seed("sub_old", "back", "sub", old, motion=1)
        main_path = self._seed("main_old", "back", "main", old, motion=1)
        # also an untouched young row of each quality
        sub_young = self._seed("sub_young", "back", "sub", 1 * 60, motion=1)
        main_young = self._seed("main_young", "back", "main", 1 * 60, motion=1)

        cfg = _make_camera_config(
            event_retain_days=1,
            continuous_days=1,
            motion_days=1,
        )

        continuous_expire = (self.now - datetime.timedelta(days=1)).timestamp()

        # run sub pass then main pass
        self.cleanup.expire_existing_camera_recordings(
            continuous_expire, continuous_expire, cfg, reviews=[]
        )
        self.cleanup.expire_existing_camera_main_recordings(cfg, self.now)

        # old: both rows + files are gone
        self.assertFalse(self._row_exists("sub_old"))
        self.assertFalse(self._exists(sub_path))
        self.assertFalse(self._row_exists("main_old"))
        self.assertFalse(self._exists(main_path))

        # young: both rows + files remain
        self.assertTrue(self._row_exists("sub_young"))
        self.assertTrue(self._exists(sub_young))
        self.assertTrue(self._row_exists("main_young"))
        self.assertTrue(self._exists(main_young))

        # invariant: every surviving DB row points at a file that exists
        for r in Recordings.select():
            self.assertTrue(
                os.path.exists(r.path),
                f"row {r.id} points to deleted file {r.path}",
            )


# =====================================================================
# test 11: event_recording.retain.days == 0 + nonzero continuous -> fallback
# =====================================================================
class TestZeroEventRetain(_DualStreamRetentionBase):
    def test_zero_event_retain_falls_back_to_continuous(self):
        """``event_recording.retain.days == 0`` is treated as "fall back to
        continuous/motion retention" (see cleanup.py lines 310-321). This is
        the documented behavior and mirrors upstream's handling of a zero
        retention value (it does not mean "delete everything instantly")."""
        old = 30 * DAY
        young = 1 * DAY
        old_path = self._seed("main_old", "back", "main", old, motion=1)
        young_path = self._seed("main_young", "back", "main", young, motion=1)

        cfg = _make_camera_config(event_retain_days=0, continuous_days=3, motion_days=3)
        self.cleanup.expire_existing_camera_main_recordings(cfg, self.now)

        # 3-day fallback drops the 30-day-old row but keeps the 1-day-old row
        self.assertFalse(self._row_exists("main_old"))
        self.assertFalse(self._exists(old_path))
        self.assertTrue(self._row_exists("main_young"))
        self.assertTrue(self._exists(young_path))


# =====================================================================
# test 12: no-valid-fallback defaults to "do not touch"
# =====================================================================
class TestNoValidFallback(_DualStreamRetentionBase):
    def test_all_retain_zero_is_noop_and_retains_main(self):
        """continuous=0, motion=0, event_recording.retain.days=0 -> the main
        pass returns an empty set without touching any row/file. This is the
        documented defensive behavior in cleanup.py (line 319-321): "If both
        event retention and the fallback are zero, treat as 'do not touch
        main segments here' rather than mass-delete". The
        ``no_camera_recordings`` pass still handles rows for deleted cameras.
        """
        old_path = self._seed("main_old", "back", "main", 30 * DAY)

        cfg = _make_camera_config(event_retain_days=0, continuous_days=0, motion_days=0)
        result = self.cleanup.expire_existing_camera_main_recordings(cfg, self.now)

        self.assertEqual(result, set())
        self.assertTrue(self._row_exists("main_old"))
        self.assertTrue(self._exists(old_path))


# =====================================================================
# test 13: sub expire pass respects stream_quality column (B1-r2 unskip)
# =====================================================================
class TestCleanupRespectsStreamQualityColumn(_DualStreamRetentionBase):
    def test_cleanup_respects_stream_quality_column(self):
        """A Recordings row with stream_quality='main' must survive the sub
        expire pass regardless of its age. This is the real DB-backed
        replacement for the previously @unittest.skip-decorated test that
        documented B1 before the fix landed."""
        old = 30 * DAY
        main_path = self._seed("main_old", "back", "main", old, motion=1)

        cfg = _make_camera_config(
            continuous_days=1, motion_days=1, event_retain_days=30
        )
        continuous_expire = (self.now - datetime.timedelta(days=1)).timestamp()
        motion_expire = continuous_expire

        # Run the sub expire pass -- it should NOT touch main rows.
        self.cleanup.expire_existing_camera_recordings(
            continuous_expire, motion_expire, cfg, reviews=[]
        )

        # The main row and its file must still exist.
        self.assertTrue(self._row_exists("main_old"))
        self.assertTrue(self._exists(main_path))


if __name__ == "__main__":
    unittest.main()
