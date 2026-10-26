"""Regression tests for HD playback query cost and API responsiveness."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from frigate.models import Recordings
from frigate.test.http_api.base_http_test import AuthTestClient, BaseTestHttp
from frigate.util import media as media_util


class TestDualStreamPlayback(BaseTestHttp):
    def setUp(self):
        super().setUp([Recordings])
        self.app = super().create_app()

    def insert_recording(self, name, start, end, quality="main", path=None):
        Recordings.insert(
            id=name,
            camera="front_door",
            path=path or f"/media/frigate/recordings/{name}.mp4",
            start_time=start,
            end_time=end,
            duration=end - start,
            stream_quality=quality,
        ).execute()

    def assert_responsive(self, path, target, slow_result):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow(*args, **kwargs):
            started.set()
            release.wait(timeout=3)
            finished.set()
            return slow_result(*args, **kwargs)

        with (
            AuthTestClient(self.app) as client,
            patch(target, side_effect=slow),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            pending = executor.submit(client.get, path)
            self.assertTrue(started.wait(timeout=2))
            try:
                response = client.get("/version")
                blocked = finished.is_set()
            finally:
                release.set()
            self.assertEqual(response.status_code, 200)
            self.assertFalse(blocked, "HD playback blocked an unrelated API request")
            self.assertEqual(pending.result().status_code, 200)

    def test_availability_does_not_block_api(self):
        select = Recordings.select
        self.assert_responsive(
            "/front_door/recordings/main_availability?after=1000&before=1010",
            "frigate.api.record.Recordings.select",
            select,
        )

    def test_vod_probe_does_not_block_api(self):
        self.insert_recording("clip", 995, 1010)
        self.assert_responsive(
            "/vod/front_door/start/1000/end/1008/quality/main",
            "frigate.api.media.get_keyframe_before",
            lambda *args: 4000,
        )

    def test_availability_retains_long_overlaps_and_bounds_index_scan(self):
        self.insert_recording("long", 401, 1000)
        self.insert_recording("hd", 1000, 1002)
        self.insert_recording("sd", 1000, 1002, "sub")
        self.insert_recording("old", 0, 2)
        with (
            AuthTestClient(self.app) as client,
            patch.object(self.db, "execute_sql", wraps=self.db.execute_sql) as execute,
        ):
            response = client.get(
                "/front_door/recordings/main_availability?after=999&before=1002"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "ranges": [
                    {"start_time": 401.0, "end_time": 1000.0},
                    {"start_time": 1000.0, "end_time": 1002.0},
                ],
                "truncated": False,
            },
        )
        sql, params = next(
            call.args[:2]
            for call in execute.call_args_list
            if call.args[0].startswith("SELECT") and '"stream_quality"' in call.args[0]
        )
        plan = self.db.execute_sql("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        detail = " ".join(row[3] for row in plan)
        self.assertIn("start_time>?", detail)
        self.assertIn("start_time<?", detail)

    def test_vod_keeps_keyframe_snap_and_legacy_main_rows(self):
        self.insert_recording("long", 401, 1000, "sub", "/media/long_main.mp4")
        self.insert_recording("sd", 999, 1002, "sub")
        with (
            AuthTestClient(self.app) as client,
            patch("frigate.api.media.get_keyframe_before", return_value=597000),
        ):
            hd = client.get("/vod/front_door/start/999/end/1000/quality/main")
            sd = client.get("/vod/front_door/start/999/end/1000")
            absent = client.get("/vod/front_door/start/2000/end/2002/quality/main")
        self.assertEqual(hd.status_code, 200)
        clip = hd.json()["sequences"][0]["clips"][0]
        self.assertEqual(clip["path"], "/media/long_main.mp4")
        self.assertEqual(clip["clipFrom"], 597000)
        self.assertEqual(hd.json()["durations"], [2000])
        self.assertNotIn("long_main.mp4", str(sd.json()))
        self.assertEqual(absent.status_code, 404)

    def test_availability_window_validation_and_camera_auth_remain(self):
        with AuthTestClient(self.app) as client:
            self.assertEqual(
                client.get(
                    "/front_door/recordings/main_availability?after=1000&before=999"
                ).status_code,
                400,
            )
            self.assertEqual(
                client.get(
                    "/front_door/recordings/main_availability?after=0&before=604801"
                ).status_code,
                400,
            )
            self.app.frigate_config.auth.roles["restricted"] = ["back_door"]
            self.assertEqual(
                client.get(
                    "/front_door/recordings/main_availability?after=999&before=1000",
                    headers={"remote-user": "viewer", "remote-role": "restricted"},
                ).status_code,
                403,
            )

    def test_keyframe_probe_cached_by_path_not_seek_offset(self):
        result = SimpleNamespace(returncode=0, stdout=b"0.0,K_\n1.0,__\n2.0,K_\n")
        with patch("frigate.util.media.sp.run", return_value=result) as probe:
            self.assertEqual(
                media_util.get_keyframe_before("/unique/cache.mp4", 1500), 0
            )
            self.assertEqual(
                media_util.get_keyframe_before("/unique/cache.mp4", 2500), 2000
            )
        self.assertEqual(probe.call_count, 1)

    def test_failed_keyframe_probe_is_retried(self):
        results = [
            SimpleNamespace(returncode=1, stdout=b""),
            SimpleNamespace(returncode=0, stdout=b"0.0,K_\n"),
        ]
        with patch("frigate.util.media.sp.run", side_effect=results) as probe:
            self.assertIsNone(media_util.get_keyframe_before("/unique/retry.mp4", 500))
            self.assertEqual(
                media_util.get_keyframe_before("/unique/retry.mp4", 500), 0
            )
        self.assertEqual(probe.call_count, 2)

    def test_keyframe_snap_does_not_round_forward(self):
        result = SimpleNamespace(returncode=0, stdout=b"0.0,K_\n0.0019,K_\n")
        with patch("frigate.util.media.sp.run", return_value=result):
            self.assertEqual(
                media_util.get_keyframe_before("/unique/fraction.mp4", 1), 0
            )
