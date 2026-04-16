"""Unit tests for dual-stream recording config validation.

Covers EventRecordingConfig defaults/bounds, the new `record_events` camera role,
and verification that the ffmpeg cmd for a `record_events` input is emitted with
the `@main@` cache path suffix.
"""

import os
import unittest

from pydantic import ValidationError

from frigate.config import FrigateConfig
from frigate.const import MAX_PRE_CAPTURE, MODEL_CACHE_DIR


class TestDualStreamConfig(unittest.TestCase):
    def setUp(self):
        # Two-input minimal config: detect+record on the substream, record_events
        # on a (separate) main-stream input. Mirrors the real intended layout.
        self.minimal_dual = {
            "mqtt": {"host": "mqtt"},
            "cameras": {
                "back": {
                    "ffmpeg": {
                        "inputs": [
                            {
                                "path": "rtsp://10.0.0.1:554/sub",
                                "roles": ["detect", "record"],
                            },
                            {
                                "path": "rtsp://10.0.0.1:554/main",
                                "roles": ["record_events"],
                            },
                        ]
                    },
                    "detect": {"height": 1080, "width": 1920, "fps": 5},
                    "record": {
                        "enabled": True,
                        "event_recording": {"enabled": True},
                    },
                }
            },
        }

        self.minimal_single = {
            "mqtt": {"host": "mqtt"},
            "cameras": {
                "back": {
                    "ffmpeg": {
                        "inputs": [
                            {
                                "path": "rtsp://10.0.0.1:554/video",
                                "roles": ["detect", "record"],
                            }
                        ]
                    },
                    "detect": {"height": 1080, "width": 1920, "fps": 5},
                    "record": {"enabled": True},
                }
            },
        }

        if not os.path.exists(MODEL_CACHE_DIR) and not os.path.islink(MODEL_CACHE_DIR):
            os.makedirs(MODEL_CACHE_DIR)

    def test_event_recording_defaults(self):
        """Default `record.event_recording` is disabled with correct capture windows."""
        frigate_config = FrigateConfig(**self.minimal_single)
        cam = frigate_config.cameras["back"]
        self.assertFalse(cam.record.event_recording.enabled)
        self.assertEqual(cam.record.event_recording.pre_capture, 15)
        self.assertEqual(cam.record.event_recording.post_capture, 10)

    def test_event_recording_post_capture_must_be_non_negative(self):
        """`post_capture = -1` should raise `ValidationError`."""
        cfg = dict(self.minimal_single)
        cfg["cameras"]["back"]["record"] = {
            "enabled": True,
            "event_recording": {"enabled": False, "post_capture": -1},
        }
        with self.assertRaises(ValidationError):
            FrigateConfig(**cfg)

    def test_event_recording_pre_capture_respects_max(self):
        """`pre_capture > MAX_PRE_CAPTURE` should raise `ValidationError`."""
        cfg = dict(self.minimal_single)
        cfg["cameras"]["back"]["record"] = {
            "enabled": True,
            "event_recording": {
                "enabled": False,
                "pre_capture": MAX_PRE_CAPTURE + 1,
            },
        }
        with self.assertRaises(ValidationError):
            FrigateConfig(**cfg)

    def test_record_events_role_and_record_on_same_input_rejected(self):
        """record and record_events on same input should be rejected."""
        cfg = {
            "mqtt": {"host": "mqtt"},
            "cameras": {
                "back": {
                    "ffmpeg": {
                        "inputs": [
                            {
                                "path": "rtsp://10.0.0.1:554/video",
                                "roles": ["detect", "record", "record_events"],
                            }
                        ]
                    },
                    "detect": {"height": 1080, "width": 1920, "fps": 5},
                    "record": {
                        "enabled": True,
                        "event_recording": {"enabled": True},
                    },
                }
            },
        }
        with self.assertRaises(ValidationError) as ctx:
            FrigateConfig(**cfg)
        self.assertIn(
            "record and record_events roles must not be on the same input",
            str(ctx.exception),
        )

    def test_record_events_role_on_separate_input_accepted(self):
        """Two inputs (detect+record + record_events) pass validation and produce a
        record_events ffmpeg cmd."""
        frigate_config = FrigateConfig(**self.minimal_dual)
        frigate_config = frigate_config.init()
        cam = frigate_config.cameras["back"]

        roles_in_cmds = [
            {
                role.value if hasattr(role, "value") else role
                for role in entry["roles"]
            }
            for entry in cam.ffmpeg_cmds
        ]
        self.assertTrue(
            any("record_events" in roles for roles in roles_in_cmds),
            f"Expected a record_events ffmpeg cmd; got roles {roles_in_cmds}",
        )

    def test_event_recording_enabled_without_record_events_role_warns(self):
        """event_recording.enabled=True but no record_events input produces no
        record_events ffmpeg cmd for that camera."""
        cfg = dict(self.minimal_single)
        cfg["cameras"]["back"]["record"] = {
            "enabled": True,
            "event_recording": {"enabled": True},
        }
        frigate_config = FrigateConfig(**cfg).init()
        cam = frigate_config.cameras["back"]

        for entry in cam.ffmpeg_cmds:
            roles = {
                role.value if hasattr(role, "value") else role
                for role in entry["roles"]
            }
            self.assertNotIn("record_events", roles)

    def test_camera_name_ending_in_at_main_rejected(self):
        """A camera whose name ends with '@main' collides with the
        '<camera>@main@<ts>.mp4' mainstream segment convention (M5) and
        must be rejected at config-load time."""
        cfg = {
            "mqtt": {"host": "mqtt"},
            "cameras": {
                "back@main": {
                    "ffmpeg": {
                        "inputs": [
                            {
                                "path": "rtsp://10.0.0.1:554/video",
                                "roles": ["detect", "record"],
                            }
                        ]
                    },
                    "detect": {"height": 1080, "width": 1920, "fps": 5},
                    "record": {"enabled": True},
                }
            },
        }
        with self.assertRaises(ValidationError):
            FrigateConfig(**cfg)

    def test_camera_name_containing_at_main_at_rejected(self):
        """A camera name containing '@main@' is also rejected because it
        would collide with substream-timestamp parsing for that camera."""
        cfg = {
            "mqtt": {"host": "mqtt"},
            "cameras": {
                "foo@main@bar": {
                    "ffmpeg": {
                        "inputs": [
                            {
                                "path": "rtsp://10.0.0.1:554/video",
                                "roles": ["detect", "record"],
                            }
                        ]
                    },
                    "detect": {"height": 1080, "width": 1920, "fps": 5},
                    "record": {"enabled": True},
                }
            },
        }
        with self.assertRaises(ValidationError):
            FrigateConfig(**cfg)

    def test_camera_name_with_at_symbol_rejected_by_regex(self):
        """REGEX_CAMERA_NAME (^[a-zA-Z0-9_-]+$) forbids '@' in camera names.
        This is the foundational guarantee that the maintainer's rsplit('@',1)
        parser cannot be fooled by a camera named e.g. 'foo@main' (M5/M3-r2).
        """
        cfg = {
            "mqtt": {"host": "mqtt"},
            "cameras": {
                "foo@main": {
                    "ffmpeg": {
                        "inputs": [
                            {
                                "path": "rtsp://10.0.0.1:554/video",
                                "roles": ["detect", "record"],
                            }
                        ]
                    },
                    "detect": {"height": 1080, "width": 1920, "fps": 5},
                    "record": {"enabled": True},
                }
            },
        }
        with self.assertRaises(ValidationError):
            FrigateConfig(**cfg)

    def test_record_events_ffmpeg_output_has_main_suffix(self):
        """The record_events ffmpeg cmd contains a cache path with `@main@`."""
        frigate_config = FrigateConfig(**self.minimal_dual).init()
        cam = frigate_config.cameras["back"]

        main_cmd = None
        for entry in cam.ffmpeg_cmds:
            roles = {
                role.value if hasattr(role, "value") else role
                for role in entry["roles"]
            }
            if "record_events" in roles:
                main_cmd = entry["cmd"]
                break

        self.assertIsNotNone(main_cmd, "Expected a record_events ffmpeg cmd")
        flat = " ".join(main_cmd)
        self.assertIn("@main@", flat)


if __name__ == "__main__":
    unittest.main()
