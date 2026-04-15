"""Run recording maintainer and cleanup."""

import logging
from multiprocessing.synchronize import Event as MpEvent

from playhouse.sqliteq import SqliteQueueDatabase

from frigate.config import FrigateConfig
from frigate.const import PROCESS_PRIORITY_HIGH
from frigate.models import Recordings, ReviewSegment
from frigate.record.event_recorder import EventRecorder
from frigate.record.maintainer import RecordingMaintainer
from frigate.util.process import FrigateProcess

logger = logging.getLogger(__name__)


class RecordProcess(FrigateProcess):
    def __init__(self, config: FrigateConfig, stop_event: MpEvent) -> None:
        super().__init__(
            stop_event,
            PROCESS_PRIORITY_HIGH,
            name="frigate.recording_manager",
            daemon=True,
        )
        self.config = config

    def run(self) -> None:
        self.pre_run_setup(self.config.logger)
        db = SqliteQueueDatabase(
            self.config.database.path,
            pragmas={
                "auto_vacuum": "FULL",  # Does not defragment database
                "cache_size": -512 * 1000,  # 512MB of cache
                "synchronous": "NORMAL",  # Safe when using WAL https://www.sqlite.org/pragma.html#pragma_synchronous
            },
            timeout=max(
                60, 10 * len([c for c in self.config.cameras.values() if c.enabled])
            ),
        )
        models = [ReviewSegment, Recordings]
        db.bind(models)

        maintainer = RecordingMaintainer(
            self.config,
            self.stop_event,
        )
        maintainer.start()

        # Start EventRecorder for cameras with record_events role enabled
        event_camera_configs = {}
        event_ffmpeg_cmds = {}
        for cam_name, cam_config in self.config.cameras.items():
            if (
                cam_config.enabled
                and cam_config.record.enabled
                and cam_config.record.event_recording.enabled
            ):
                # find the ffmpeg cmd for the record_events role
                found_role = False
                for cmd_entry in cam_config.ffmpeg_cmds:
                    if "record_events" in cmd_entry["roles"]:
                        event_camera_configs[cam_name] = cam_config
                        event_ffmpeg_cmds[cam_name] = cmd_entry["cmd"]
                        found_role = True
                        break
                if not found_role:
                    logger.warning(
                        f"Camera {cam_name} has event_recording enabled but no "
                        f"record_events role configured in ffmpeg inputs. "
                        f"Add a record_events role pointing to the main stream."
                    )

        if event_camera_configs:
            logger.info(
                f"Starting event recorder for cameras: {list(event_camera_configs.keys())}"
            )
            event_recorder = EventRecorder(
                self.config,
                event_camera_configs,
                event_ffmpeg_cmds,
                self.stop_event,
            )
            event_recorder.start()
