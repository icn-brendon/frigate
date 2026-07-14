"""Unit tests for export stream-quality selection (dual-stream recording).

Exports must come from the HD (main) event stream when main footage covers
the requested range, instead of always defaulting to the sub stream. These
tests exercise ``RecordingExporter.resolve_stream_quality`` and
``get_record_export_command`` against a real in-memory SQLite via peewee,
mirroring the fixture style of ``test_retention_dual_stream.py``.
"""

import tempfile
import unittest
from unittest.mock import MagicMock, patch

from playhouse.sqlite_ext import SqliteExtDatabase

from frigate.const import MAX_PLAYLIST_SECONDS
from frigate.models import Recordings
from frigate.record.export import (
    ExportQualityEnum,
    PlaybackSourceEnum,
    RecordingExporter,
)

BASE_TS = 1_700_000_000


class _ExportQualityBase(unittest.TestCase):
    """Shared fixture: in-memory SQLite plus a RecordingExporter factory."""

    def setUp(self):
        self.db = SqliteExtDatabase(":memory:")
        self._models = [Recordings]
        self.db.bind(self._models)
        self.db.connect()
        self.db.create_tables(self._models)

        # RecordingExporter.__init__ creates the export thumbnail dir under
        # CLIPS_DIR, which doesn't exist on dev machines — point it at a tmp.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self):
        if not self.db.is_closed():
            self.db.close()

    def _seed(
        self,
        rec_id: str,
        stream_quality: str,
        start_time: float,
        end_time: float,
        camera: str = "back",
        path: str | None = None,
    ) -> None:
        """Insert a Recordings row. The path suffix follows move_segment
        conventions (``_main.mp4`` for main rows) unless overridden, so the
        path-based legacy fallback can be exercised independently of the
        stream_quality column."""
        if path is None:
            suffix = "_main" if stream_quality == "main" else ""
            path = f"/media/frigate/recordings/{camera}/{rec_id}{suffix}.mp4"

        Recordings.create(
            id=rec_id,
            camera=camera,
            path=path,
            start_time=start_time,
            end_time=end_time,
            duration=end_time - start_time,
            stream_quality=stream_quality,
        )

    def _make_exporter(
        self,
        start_time: int,
        end_time: int,
        quality: str = ExportQualityEnum.auto.value,
    ) -> RecordingExporter:
        config = MagicMock()
        config.networking.listen.internal = 5000
        config.ffmpeg.ffmpeg_path = "ffmpeg"

        with patch("frigate.record.export.CLIPS_DIR", self._tmp.name):
            return RecordingExporter(
                config,
                "back_abc123",
                "back",
                None,
                None,
                start_time,
                end_time,
                PlaybackSourceEnum.recordings,
                quality=quality,
            )


class TestResolveStreamQuality(_ExportQualityBase):
    def test_auto_resolves_main_when_main_overlaps(self):
        self._seed("sub1", "sub", BASE_TS, BASE_TS + 60)
        self._seed("main1", "main", BASE_TS + 10, BASE_TS + 30)

        exporter = self._make_exporter(BASE_TS, BASE_TS + 60)
        self.assertEqual(
            exporter.resolve_stream_quality(), ExportQualityEnum.main.value
        )

    def test_auto_resolves_sub_when_no_main(self):
        self._seed("sub1", "sub", BASE_TS, BASE_TS + 60)

        exporter = self._make_exporter(BASE_TS, BASE_TS + 60)
        self.assertEqual(exporter.resolve_stream_quality(), ExportQualityEnum.sub.value)

    def test_auto_ignores_main_outside_range(self):
        self._seed("sub1", "sub", BASE_TS, BASE_TS + 60)
        # main footage exists, but only outside the requested range
        self._seed("main1", "main", BASE_TS + 600, BASE_TS + 700)

        exporter = self._make_exporter(BASE_TS, BASE_TS + 60)
        self.assertEqual(exporter.resolve_stream_quality(), ExportQualityEnum.sub.value)

    def test_auto_ignores_main_on_other_camera(self):
        self._seed("sub1", "sub", BASE_TS, BASE_TS + 60)
        self._seed("main1", "main", BASE_TS, BASE_TS + 60, camera="front")

        exporter = self._make_exporter(BASE_TS, BASE_TS + 60)
        self.assertEqual(exporter.resolve_stream_quality(), ExportQualityEnum.sub.value)

    def test_auto_detects_legacy_main_row_by_path_suffix(self):
        # Rows inserted before the maintainer was fixed can carry the
        # column default "sub" while the file is clearly main. The main
        # predicate must match on the _main.mp4 suffix (same as vod_ts).
        self._seed(
            "legacy1",
            "sub",
            BASE_TS,
            BASE_TS + 60,
            path="/media/frigate/recordings/back/legacy1_main.mp4",
        )

        exporter = self._make_exporter(BASE_TS, BASE_TS + 60)
        self.assertEqual(
            exporter.resolve_stream_quality(), ExportQualityEnum.main.value
        )

    def test_explicit_sub_stays_sub_even_with_main_footage(self):
        self._seed("main1", "main", BASE_TS, BASE_TS + 60)

        exporter = self._make_exporter(
            BASE_TS, BASE_TS + 60, quality=ExportQualityEnum.sub.value
        )
        self.assertEqual(exporter.resolve_stream_quality(), ExportQualityEnum.sub.value)

    def test_explicit_main_stays_main_even_without_footage(self):
        exporter = self._make_exporter(
            BASE_TS, BASE_TS + 60, quality=ExportQualityEnum.main.value
        )
        self.assertEqual(
            exporter.resolve_stream_quality(), ExportQualityEnum.main.value
        )

    def test_resolution_is_cached(self):
        self._seed("main1", "main", BASE_TS, BASE_TS + 60)

        exporter = self._make_exporter(BASE_TS, BASE_TS + 60)
        self.assertEqual(
            exporter.resolve_stream_quality(), ExportQualityEnum.main.value
        )

        # deleting the rows must not flip the decision mid-export
        # (cpu-fallback retries reuse the cached quality)
        Recordings.delete().execute()
        self.assertEqual(
            exporter.resolve_stream_quality(), ExportQualityEnum.main.value
        )


class TestShortExportCommand(_ExportQualityBase):
    """Ranges <= MAX_PLAYLIST_SECONDS use a single vod playlist URL."""

    def test_main_quality_uses_path_embedded_quality_url(self):
        start = BASE_TS
        end = BASE_TS + 60
        self._seed("main1", "main", start, end)

        exporter = self._make_exporter(start, end)
        ffmpeg_cmd, playlist_lines = exporter.get_record_export_command("/tmp/out.mp4")

        # quality must be path-embedded so nginx-vod-module subrequests
        # forward it (see vod_ts_with_quality in api/media.py)
        self.assertIn(
            f"http://127.0.0.1:5000/vod/back/start/{start}/end/{end}/quality/main/index.m3u8",
            ffmpeg_cmd,
        )
        self.assertEqual(playlist_lines, [])

    def test_sub_quality_keeps_legacy_url(self):
        start = BASE_TS
        end = BASE_TS + 60
        self._seed("sub1", "sub", start, end)

        exporter = self._make_exporter(start, end)
        ffmpeg_cmd, _ = exporter.get_record_export_command("/tmp/out.mp4")

        self.assertIn(
            f"http://127.0.0.1:5000/vod/back/start/{start}/end/{end}/index.m3u8",
            ffmpeg_cmd,
        )
        self.assertFalse(any("/quality/" in arg for arg in ffmpeg_cmd))

    def test_explicit_sub_url_ignores_main_footage(self):
        start = BASE_TS
        end = BASE_TS + 60
        self._seed("main1", "main", start, end)

        exporter = self._make_exporter(start, end, quality=ExportQualityEnum.sub.value)
        ffmpeg_cmd, _ = exporter.get_record_export_command("/tmp/out.mp4")

        self.assertFalse(any("/quality/" in arg for arg in ffmpeg_cmd))


class TestLongExportCommand(_ExportQualityBase):
    """Ranges > MAX_PLAYLIST_SECONDS build a concat playlist from a
    Recordings query, which must be filtered to the resolved quality so
    chunk boundaries don't mix main and sub rows."""

    def test_long_export_main_filters_query_and_uses_quality_url(self):
        start = BASE_TS
        end = BASE_TS + MAX_PLAYLIST_SECONDS + 600

        # sub rows span the whole range; main rows only cover a slice.
        # If the query weren't filtered, the playlist boundaries would be
        # taken from the sub rows.
        self._seed("sub1", "sub", start, end)
        main_start = start + 100
        main_end = start + 200
        self._seed("main1", "main", main_start, main_end)

        exporter = self._make_exporter(start, end)
        _, playlist_lines = exporter.get_record_export_command("/tmp/out.mp4")

        self.assertEqual(
            playlist_lines,
            [
                f"file 'http://127.0.0.1:5000/vod/back/start/{float(main_start)}/end/{float(main_end)}/quality/main/index.m3u8'"
            ],
        )

    def test_long_export_sub_filters_out_main_rows(self):
        start = BASE_TS
        end = BASE_TS + MAX_PLAYLIST_SECONDS + 600

        sub_start = start + 50
        sub_end = start + 500
        self._seed("sub1", "sub", sub_start, sub_end)
        # main row extends past the sub row; it must not stretch the
        # sub-quality playlist boundaries
        self._seed("main1", "main", start, end)

        exporter = self._make_exporter(start, end, quality=ExportQualityEnum.sub.value)
        _, playlist_lines = exporter.get_record_export_command("/tmp/out.mp4")

        self.assertEqual(
            playlist_lines,
            [
                f"file 'http://127.0.0.1:5000/vod/back/start/{float(sub_start)}/end/{float(sub_end)}/index.m3u8'"
            ],
        )


if __name__ == "__main__":
    unittest.main()
