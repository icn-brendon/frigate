"""Record main stream segments during motion/detection events."""

import logging
import subprocess as sp
import threading
import time
from multiprocessing.synchronize import Event as MpEvent
from typing import Any, Optional

from frigate.comms.detections_updater import DetectionSubscriber, DetectionTypeEnum
from frigate.config import CameraConfig, FrigateConfig
from frigate.const import CACHE_DIR, CACHE_SEGMENT_FORMAT, FAST_QUEUE_TIMEOUT
from frigate.log import LogPipe
from frigate.util.ffmpeg import start_or_restart_ffmpeg, stop_ffmpeg

logger = logging.getLogger(__name__)


class CameraRecordingState:
    """Tracks the recording state for a single camera."""

    def __init__(self) -> None:
        self.is_recording: bool = False
        self.ffmpeg_process: Optional[sp.Popen[Any]] = None
        self.last_activity_time: float = 0.0
        self.recording_start_time: float = 0.0
        self.logpipe: Optional[LogPipe] = None


class EventRecorder(threading.Thread):
    """Records main stream segments during motion/detection events.

    This thread subscribes to detection events and manages FFmpeg processes
    that record the main stream to cache only when motion or object detections
    are active. When no activity is detected for the configured post_capture
    duration, the FFmpeg process is stopped, which causes go2rtc to disconnect
    the upstream RTSP connection and save bandwidth.

    Trade-off: Because the main stream FFmpeg process is started reactively
    when the first detection arrives, the initial ~1-2 seconds of an event
    may only have substream coverage. True pre_capture for the main stream
    would require keeping the process running continuously, which defeats
    the purpose of event-only recording. The substream continuous recording
    provides coverage for this gap.
    """

    def __init__(
        self,
        config: FrigateConfig,
        camera_configs: dict[str, CameraConfig],
        ffmpeg_cmds: dict[str, list[str]],
        stop_event: MpEvent,
    ) -> None:
        super().__init__(name="event_recorder", daemon=True)
        self.config = config
        self.camera_configs = camera_configs
        self.ffmpeg_cmds = ffmpeg_cmds
        self.stop_event = stop_event

        self.detection_subscriber = DetectionSubscriber(
            DetectionTypeEnum.video.value
        )

        # per-camera recording state
        self.camera_states: dict[str, CameraRecordingState] = {}
        for camera in camera_configs:
            self.camera_states[camera] = CameraRecordingState()

    def run(self) -> None:
        """Main loop: consume detection events and manage FFmpeg processes."""
        while not self.stop_event.is_set():
            self._process_detection_events()
            self._check_timeouts()
            self._check_ffmpeg_health()

            if self.stop_event.wait(0.5):
                break

        self._stop_all_recordings()
        self.detection_subscriber.stop()
        logger.info("Exiting event recorder...")

    def _process_detection_events(self) -> None:
        """Drain all pending detection events and update activity times."""
        while True:
            result = self.detection_subscriber.check_for_update(
                timeout=FAST_QUEUE_TIMEOUT
            )

            if not result:
                break

            topic, data = result

            if not topic or not data:
                break

            (
                camera,
                _,
                frame_time,
                current_tracked_objects,
                motion_boxes,
                regions,
            ) = data

            if camera not in self.camera_states:
                continue

            # determine if there is meaningful activity in this frame
            has_motion = len(motion_boxes) > 0
            has_objects = len(
                [
                    o
                    for o in current_tracked_objects
                    if not o["false_positive"] and o["motionless_count"] == 0
                ]
            ) > 0

            if has_motion or has_objects:
                state = self.camera_states[camera]
                # use wall clock time for timeout comparison consistency
                state.last_activity_time = time.time()

                if not state.is_recording:
                    self._start_recording(camera)

    def _start_recording(self, camera: str) -> None:
        """Start FFmpeg process for this camera's main stream."""
        state = self.camera_states[camera]

        if camera not in self.ffmpeg_cmds:
            logger.error(
                f"No FFmpeg command configured for event recording on {camera}"
            )
            return

        state.logpipe = LogPipe(f"ffmpeg.{camera}.event_record")
        state.ffmpeg_process = start_or_restart_ffmpeg(
            self.ffmpeg_cmds[camera],
            logger,
            state.logpipe,
        )
        state.is_recording = True
        state.recording_start_time = time.time()

        logger.info(
            f"Started main stream event recording for {camera} (pid {state.ffmpeg_process.pid})"
        )

    def _stop_recording(self, camera: str) -> None:
        """Stop FFmpeg process for this camera's main stream."""
        state = self.camera_states[camera]

        if state.ffmpeg_process is not None:
            stop_ffmpeg(state.ffmpeg_process, logger)
            state.ffmpeg_process = None

        duration = time.time() - state.recording_start_time
        logger.info(
            f"Stopped main stream event recording for {camera} "
            f"(ran for {duration:.1f}s)"
        )

        if state.logpipe is not None:
            state.logpipe.close()
            state.logpipe = None

        state.is_recording = False
        state.recording_start_time = 0.0

    def _check_timeouts(self) -> None:
        """Stop recording for cameras where post_capture has elapsed."""
        now = time.time()

        for camera, state in self.camera_states.items():
            if not state.is_recording:
                continue

            if state.last_activity_time <= 0:
                continue

            post_capture = (
                self.camera_configs[camera].record.event_recording.post_capture
            )
            elapsed = now - state.last_activity_time

            if elapsed >= post_capture:
                logger.debug(
                    f"Post-capture timeout ({post_capture}s) reached for {camera}, "
                    f"stopping main stream recording"
                )
                self._stop_recording(camera)

    def _check_ffmpeg_health(self) -> None:
        """Check for crashed FFmpeg processes and restart if needed."""
        for camera, state in self.camera_states.items():
            if not state.is_recording or state.ffmpeg_process is None:
                continue

            poll = state.ffmpeg_process.poll()

            if poll is not None:
                logger.warning(
                    f"FFmpeg event recording process for {camera} exited "
                    f"unexpectedly with code {poll}"
                )

                if state.logpipe is not None:
                    state.logpipe.dump()

                # only restart if there is still recent activity
                now = time.time()
                post_capture = (
                    self.camera_configs[camera].record.event_recording.post_capture
                )

                if (now - state.last_activity_time) < post_capture:
                    logger.info(
                        f"Restarting FFmpeg event recording for {camera} "
                        f"(activity still within post_capture window)"
                    )
                    # create a fresh logpipe for the restarted process
                    if state.logpipe is not None:
                        state.logpipe.close()
                    state.logpipe = LogPipe(f"ffmpeg.{camera}.event_record")
                    state.ffmpeg_process = start_or_restart_ffmpeg(
                        self.ffmpeg_cmds[camera],
                        logger,
                        state.logpipe,
                    )
                    logger.info(
                        f"Restarted main stream event recording for {camera} "
                        f"(pid {state.ffmpeg_process.pid})"
                    )
                else:
                    # activity has expired, just clean up
                    state.ffmpeg_process = None
                    state.is_recording = False
                    state.recording_start_time = 0.0

                    if state.logpipe is not None:
                        state.logpipe.close()
                        state.logpipe = None

    def _stop_all_recordings(self) -> None:
        """Gracefully stop all active FFmpeg processes on shutdown."""
        for camera, state in self.camera_states.items():
            if state.is_recording:
                logger.info(
                    f"Shutting down: stopping main stream event recording for {camera}"
                )
                self._stop_recording(camera)
