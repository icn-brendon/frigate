"""Unit tests for the unified VOD timeline builder.

``build_unified_vod_timeline`` is the pure helper behind ``vod_ts`` in
``frigate.api.media``. It must produce monotonic non-overlapping clips:
nginx-vod-module otherwise returns 400 on segment fetches.
"""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

# The media module imports heavy optional deps (FastAPI, cv2, peewee models
# bound to a DB, etc.) at import time. Stub the ones that are not needed for
# the pure-Python timeline helper.
_STUBBED = [
    "cv2",
    "numpy",
    "pytz",
    "tzlocal",
    "pathvalidate",
    "fastapi",
    "fastapi.responses",
    "peewee",
    "frigate.api.auth",
    "frigate.api.defs.query.media_query_parameters",
    "frigate.api.defs.tags",
    "frigate.camera.state",
    "frigate.config",
    "frigate.config.camera.snapshots",
    "frigate.const",
    "frigate.models",
    "frigate.output.preview",
    "frigate.track.object_processing",
    "frigate.util.file",
    "frigate.util.image",
    "frigate.util.media",
]
_originals = {name: sys.modules.get(name) for name in _STUBBED}
for name in _STUBBED:
    sys.modules.setdefault(name, MagicMock())

from frigate.api.media import build_unified_vod_timeline  # noqa: E402

for name, orig in _originals.items():
    if orig is None:
        # Keep the stub in place; tearing it down could break later imports
        # in the same test process.
        continue
    sys.modules[name] = orig


def _row(path: str, start: float, end: float, quality: str = "sub"):
    return SimpleNamespace(
        path=path,
        start_time=start,
        end_time=end,
        duration=end - start,
        stream_quality=quality,
    )


def _assert_non_overlapping(test, emissions):
    for a, b in zip(emissions, emissions[1:]):
        test.assertLessEqual(
            a["trim_end_wall"],
            b["trim_start_wall"],
            msg=f"Overlap between {a} and {b}",
        )


class TestUnifiedVodTimeline(unittest.TestCase):
    def test_main_inside_sub_splits_into_pre_main_post(self):
        """2s main clip fully inside a 4s sub clip should produce
        [sub_pre, main, sub_post] with no overlap."""
        sub = _row("/rec/sub.mp4", 1000.0, 1004.0, quality="sub")
        main = _row("/rec/main.mp4", 1001.0, 1003.0, quality="main")

        emissions = build_unified_vod_timeline(
            main_rows=[main], sub_rows=[sub], after=1000.0, before=1004.0
        )

        self.assertEqual(len(emissions), 3)
        _assert_non_overlapping(self, emissions)

        pre, mid, post = emissions
        self.assertEqual(pre["row"].path, "/rec/sub.mp4")
        self.assertEqual(pre["trim_start_wall"], 1000.0)
        self.assertEqual(pre["trim_end_wall"], 1001.0)
        self.assertEqual(pre["clip_from_ms"], 0)
        self.assertEqual(pre["duration_ms"], 1000)

        self.assertEqual(mid["row"].path, "/rec/main.mp4")
        self.assertEqual(mid["trim_start_wall"], 1001.0)
        self.assertEqual(mid["trim_end_wall"], 1003.0)
        self.assertEqual(mid["clip_from_ms"], 0)
        self.assertEqual(mid["duration_ms"], 2000)

        self.assertEqual(post["row"].path, "/rec/sub.mp4")
        self.assertEqual(post["trim_start_wall"], 1003.0)
        self.assertEqual(post["trim_end_wall"], 1004.0)
        self.assertEqual(post["clip_from_ms"], 3000)
        self.assertEqual(post["duration_ms"], 1000)

    def test_two_main_with_sub_spanning_both_and_gap(self):
        """Sub spans the whole window; two mains with a gap between them.
        Output should weave: main, sub(gap), main, (no trailing sub because
        sub ends with the second main)."""
        sub = _row("/rec/sub.mp4", 2000.0, 2010.0, quality="sub")
        main_a = _row("/rec/a.mp4", 2001.0, 2003.0, quality="main")
        main_b = _row("/rec/b.mp4", 2005.0, 2010.0, quality="main")

        emissions = build_unified_vod_timeline(
            main_rows=[main_a, main_b],
            sub_rows=[sub],
            after=2000.0,
            before=2010.0,
        )

        _assert_non_overlapping(self, emissions)
        # Expect: sub[2000,2001], main_a[2001,2003], sub[2003,2005],
        # main_b[2005,2010]
        self.assertEqual(len(emissions), 4)

        self.assertEqual(emissions[0]["row"].path, "/rec/sub.mp4")
        self.assertEqual(emissions[0]["trim_start_wall"], 2000.0)
        self.assertEqual(emissions[0]["trim_end_wall"], 2001.0)
        self.assertEqual(emissions[0]["clip_from_ms"], 0)
        self.assertEqual(emissions[0]["duration_ms"], 1000)

        self.assertEqual(emissions[1]["row"].path, "/rec/a.mp4")
        self.assertEqual(emissions[1]["trim_start_wall"], 2001.0)
        self.assertEqual(emissions[1]["trim_end_wall"], 2003.0)

        self.assertEqual(emissions[2]["row"].path, "/rec/sub.mp4")
        self.assertEqual(emissions[2]["trim_start_wall"], 2003.0)
        self.assertEqual(emissions[2]["trim_end_wall"], 2005.0)
        # clipFrom into sub source file at 2003 - 2000 = 3s
        self.assertEqual(emissions[2]["clip_from_ms"], 3000)
        self.assertEqual(emissions[2]["duration_ms"], 2000)

        self.assertEqual(emissions[3]["row"].path, "/rec/b.mp4")
        self.assertEqual(emissions[3]["trim_start_wall"], 2005.0)
        self.assertEqual(emissions[3]["trim_end_wall"], 2010.0)

    def test_only_sub_no_main(self):
        """With no main rows all sub rows should be emitted (trimmed to the
        requested window) with no overlap."""
        sub_a = _row("/rec/s1.mp4", 3000.0, 3004.0, quality="sub")
        sub_b = _row("/rec/s2.mp4", 3004.0, 3008.0, quality="sub")

        emissions = build_unified_vod_timeline(
            main_rows=[], sub_rows=[sub_a, sub_b], after=3000.0, before=3008.0
        )

        _assert_non_overlapping(self, emissions)
        self.assertEqual(len(emissions), 2)
        self.assertEqual(emissions[0]["row"].path, "/rec/s1.mp4")
        self.assertEqual(emissions[0]["duration_ms"], 4000)
        self.assertEqual(emissions[1]["row"].path, "/rec/s2.mp4")
        self.assertEqual(emissions[1]["duration_ms"], 4000)

    def test_only_main_produces_visible_gaps(self):
        """With main rows and no sub fallback, gaps remain (acceptable)."""
        main_a = _row("/rec/a.mp4", 4000.0, 4002.0, quality="main")
        main_b = _row("/rec/b.mp4", 4005.0, 4007.0, quality="main")

        emissions = build_unified_vod_timeline(
            main_rows=[main_a, main_b],
            sub_rows=[],
            after=4000.0,
            before=4010.0,
        )

        _assert_non_overlapping(self, emissions)
        self.assertEqual(len(emissions), 2)
        self.assertEqual(emissions[0]["trim_start_wall"], 4000.0)
        self.assertEqual(emissions[0]["trim_end_wall"], 4002.0)
        self.assertEqual(emissions[1]["trim_start_wall"], 4005.0)
        self.assertEqual(emissions[1]["trim_end_wall"], 4007.0)

    def test_overlapping_main_rows_are_trimmed_not_duplicated(self):
        """Two main rows that overlap (e.g. an event-burst continuation) must
        not produce overlapping clips in the emitted timeline."""
        main_a = _row("/rec/a.mp4", 5000.0, 5003.0, quality="main")
        main_b = _row("/rec/b.mp4", 5002.0, 5005.0, quality="main")

        emissions = build_unified_vod_timeline(
            main_rows=[main_a, main_b],
            sub_rows=[],
            after=5000.0,
            before=5005.0,
        )

        _assert_non_overlapping(self, emissions)
        self.assertEqual(len(emissions), 2)
        self.assertEqual(emissions[0]["trim_start_wall"], 5000.0)
        self.assertEqual(emissions[0]["trim_end_wall"], 5003.0)
        self.assertEqual(emissions[1]["trim_start_wall"], 5003.0)
        self.assertEqual(emissions[1]["trim_end_wall"], 5005.0)
        # The second clip has to skip the portion already covered by main_a.
        self.assertEqual(emissions[1]["clip_from_ms"], 1000)
        self.assertEqual(emissions[1]["duration_ms"], 2000)

    def test_request_window_trims_outer_bounds(self):
        """Sub rows that extend beyond [after, before] must be trimmed."""
        sub = _row("/rec/s.mp4", 6000.0, 6010.0, quality="sub")

        emissions = build_unified_vod_timeline(
            main_rows=[], sub_rows=[sub], after=6002.0, before=6008.0
        )

        self.assertEqual(len(emissions), 1)
        e = emissions[0]
        self.assertEqual(e["trim_start_wall"], 6002.0)
        self.assertEqual(e["trim_end_wall"], 6008.0)
        self.assertEqual(e["clip_from_ms"], 2000)
        self.assertEqual(e["duration_ms"], 6000)


if __name__ == "__main__":
    unittest.main()
