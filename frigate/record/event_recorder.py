"""Record main stream segments during motion/detection events.

The mainstream ffmpeg process for a camera with the ``record_events`` role
runs **continuously**, writing short (~2 s) segments into a per-camera ring
buffer directory under ``CACHE_DIR/event_buffer/{camera}/``. The
``EventRecorder`` thread:

* drains those segments and, when no event is active, prunes anything older
  than ``record.event_recording.pre_capture`` seconds (the ring-buffer
  retention window);
* when a detection trigger fires, **promotes** every still-buffered segment
  (i.e. up to ``pre_capture`` seconds of pre-roll) into the flat
  ``CACHE_DIR`` as ``camera@main@{ts}.mp4`` files, where the
  ``RecordingMaintainer`` picks them up like any other main segment;
* keeps promoting freshly produced segments while activity continues, and
  for ``post_capture`` seconds after the trigger clears;
* on clear, returns to ring-buffer-only mode (continued pruning).

This replaces the previous reactive design that started/stopped the
mainstream ffmpeg process on every event — the reactive version could not
provide any meaningful ``pre_capture`` because the process did not exist
before the trigger arrived.
"""

import logging
import os
import shutil
import threading
import time
from multiprocessing.synchronize import Event as MpEvent
from pathlib import Path
from typing import Optional

from frigate.comms.detections_updater import DetectionSubscriber, DetectionTypeEnum
from frigate.config import CameraConfig, FrigateConfig
from frigate.const import (
    CACHE_DIR,
    CACHE_SEGMENT_FORMAT,
    EVENT_BUFFER_BASE_DIR,
    FAST_QUEUE_TIMEOUT,
)

logger = logging.getLogger(__name__)


def _camera_buffer_dir(camera: str) -> str:
    return os.path.join(EVENT_BUFFER_BASE_DIR, camera)


class CameraRecordingState:
    """Tracks the event-recording state for a single camera."""

    def __init__(self) -> None:
        # Wall-clock time of the most recent meaningful detection. Used to
        # decide when post_capture has elapsed.
        self.last_activity_time: float = 0.0
        # True between trigger arrival and post_capture timeout.
        self.is_active: bool = False
        # Set of buffer-directory file paths that have already been
        # promoted to CACHE_DIR (so we never promote the same file twice).
        self.promoted_paths: set[str] = set()


class EventRecorder(threading.Thread):
    """Manage the per-camera ring buffers of mainstream segments and
    promote them into the recording cache during detection events.

    Note: this thread does NOT spawn or kill any ffmpeg process. The
    mainstream ffmpeg with the ``record_events`` role is started by the
    normal camera ffmpeg lifecycle and writes continuously into the
    ring-buffer subdirectory.
    """

    # How frequently to scan the per-camera buffer directories.
    POLL_INTERVAL_SECONDS = 1.0

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
        # ffmpeg_cmds is retained for compatibility with the previous
        # interface but is no longer used: ffmpeg is owned by the camera
        # process now.
        self.ffmpeg_cmds = ffmpeg_cmds
        self.stop_event = stop_event

        self.detection_subscriber = DetectionSubscriber(
            DetectionTypeEnum.video.value
        )

        self.camera_states: dict[str, CameraRecordingState] = {
            camera: CameraRecordingState() for camera in camera_configs
        }

        # Ensure buffer directories exist (also created in camera config,
        # but defensive here for restarts and tests).
        for camera in camera_configs:
            try:
                os.makedirs(_camera_buffer_dir(camera), exist_ok=True)
            except OSError as e:
                logger.warning(
                    f"Could not create event buffer dir for {camera}: {e}"
                )

    # ---------------------------- main loop ----------------------------

    def run(self) -> None:
        while not self.stop_event.is_set():
            self._process_detection_events()
            self._tick_buffers()

            if self.stop_event.wait(self.POLL_INTERVAL_SECONDS):
                break

        self.detection_subscriber.stop()
        logger.info("Exiting event recorder...")

    # ----------------------- detection ingestion -----------------------

    def _process_detection_events(self) -> None:
        """Drain pending detection events, update activity timestamps and
        flip cameras into the ``is_active`` state on a fresh trigger."""
        while True:
            result = self.detection_subscriber.check_for_update(
                timeout=FAST_QUEUE_TIMEOUT
            )

            if not result:
                break

            topic, data = result

            if not topic or not data:
                break

            try:
                (
                    camera,
                    _,
                    _frame_time,
                    current_tracked_objects,
                    motion_boxes,
                    _regions,
                ) = data
            except (TypeError, ValueError):
                logger.debug("Ignoring detection event with unexpected shape")
                continue

            if camera not in self.camera_states:
                continue

            has_motion = len(motion_boxes) > 0
            obj_filters = self.camera_configs[camera].objects.filters
            camera_fps = self.camera_configs[camera].detect.fps
            has_objects = (
                len(
                    [
                        o
                        for o in current_tracked_objects
                        if not o["false_positive"]
                        and self._is_object_active(o, obj_filters, camera_fps)
                    ]
                )
                > 0
            )

            if has_motion or has_objects:
                state = self.camera_states[camera]
                state.last_activity_time = time.time()
                if not state.is_active:
                    state.is_active = True
                    logger.info(
                        f"Event trigger for {camera}: promoting up to "
                        f"{self.camera_configs[camera].record.event_recording.pre_capture}s "
                        "of mainstream ring buffer"
                    )

    @staticmethod
    def _is_object_active(
        obj: dict, obj_filters: dict, camera_fps: int
    ) -> bool:
        """Return True if the tracked object should be treated as active
        (i.e. should trigger or sustain mainstream event recording).

        An object with ``motionless_count == 0`` is always active.  For
        stationary objects (motionless_count > 0), we check two things:

        1. ``stationary_trigger_recording`` must be enabled for the label.
        2. If ``stationary_recording_threshold`` (seconds) is set on the
           filter, the object is only considered *stationary* once
           ``motionless_count >= threshold * fps``.  Until that frame
           count is reached the object is still treated as active.
        """
        motionless = obj.get("motionless_count", 0)
        if motionless == 0:
            return True

        label = obj.get("label")
        if label not in obj_filters:
            return False

        filt = obj_filters[label]
        if not filt.stationary_trigger_recording:
            return False

        # If a seconds-based threshold is configured, the object is still
        # "active" until it has been motionless for that many seconds.
        threshold_sec = getattr(filt, "stationary_recording_threshold", None)
        if threshold_sec is not None:
            threshold_frames = threshold_sec * max(camera_fps, 1)
            if motionless < threshold_frames:
                return True  # not yet stationary — still active

        # stationary_trigger_recording is True, so stationary objects
        # still trigger recording.
        return True

    # ------------------------ buffer maintenance -----------------------

    def _tick_buffers(self) -> None:
        """For every camera, either prune the ring buffer to ``pre_capture``
        seconds (idle) or promote new segments to the persistent cache
        (active). Also handles transitioning out of the active state once
        ``post_capture`` seconds have elapsed since the last trigger."""
        now = time.time()

        for camera, state in self.camera_states.items():
            cfg = self.camera_configs[camera].record.event_recording
            pre_capture = cfg.pre_capture
            post_capture = cfg.post_capture

            buffer_dir = _camera_buffer_dir(camera)
            if not os.path.isdir(buffer_dir):
                continue

            # Build a sorted list of (mtime, path) for files in the buffer.
            entries: list[tuple[float, str]] = []
            for name in os.listdir(buffer_dir):
                if not name.endswith(".mp4"):
                    continue
                full = os.path.join(buffer_dir, name)
                try:
                    entries.append((os.path.getmtime(full), full))
                except OSError:
                    continue
            entries.sort()

            # Determine whether we are still in the active window.
            if state.is_active:
                if (
                    state.last_activity_time > 0
                    and (now - state.last_activity_time) >= post_capture
                ):
                    state.is_active = False
                    state.promoted_paths.clear()
                    logger.info(
                        f"Post-capture window ({post_capture}s) elapsed for "
                        f"{camera}; returning to ring-buffer-only mode"
                    )

            if state.is_active:
                # Promote everything currently in the buffer that we have
                # not already promoted. We skip the newest file because
                # ffmpeg may still be writing to it.
                promotable = entries[:-1] if len(entries) > 1 else []
                for _mtime, src_path in promotable:
                    if src_path in state.promoted_paths:
                        continue
                    self._promote_segment(camera, src_path, state)
            else:
                # Idle: prune anything older than the pre_capture window.
                cutoff = now - max(0, pre_capture)
                # Always keep at least the most recent file, even if it is
                # older than the cutoff (otherwise on a very low-fps stream
                # we could end up with an empty buffer right when an event
                # arrives).
                for _mtime, path in entries[:-1] if entries else []:
                    if _mtime < cutoff:
                        try:
                            Path(path).unlink(missing_ok=True)
                        except OSError as e:
                            logger.debug(
                                f"Failed to prune buffer segment {path}: {e}"
                            )

    def _promote_segment(
        self, camera: str, src_path: str, state: CameraRecordingState
    ) -> None:
        """Move a buffered segment into the flat CACHE_DIR with the
        ``camera@main@ts.mp4`` naming convention so the maintainer treats
        it as a persistent main-stream segment.

        The timestamp is parsed from the buffer filename itself (which
        ffmpeg produced with ``-strftime 1`` using CACHE_SEGMENT_FORMAT),
        NOT from ``os.path.getmtime``. mtime is the write-completion time
        and runs ~segment-duration seconds behind the actual start-of-
        content, which caused ``vod_ts`` to fall back to SD for the first
        ~2 s of every event (M8).
        """
        # Buffer filename is "<CACHE_SEGMENT_FORMAT>.mp4" i.e. the base
        # name IS the start timestamp. Use it verbatim so the maintainer
        # ingests with a start_time matching the segment content, not the
        # file-system write completion time.
        base = os.path.splitext(os.path.basename(src_path))[0]
        # Validate by round-tripping through the configured format; if the
        # filename isn't a valid strftime match we fall back to mtime to
        # preserve recoverability rather than dropping the segment.
        try:
            import datetime as _dt

            _dt.datetime.strptime(base, CACHE_SEGMENT_FORMAT)
            ts_str = base
        except ValueError:
            logger.debug(
                f"Buffer segment {src_path} has non-strftime name; "
                "falling back to mtime for promotion timestamp"
            )
            try:
                mtime = os.path.getmtime(src_path)
            except OSError:
                return
            ts_struct = time.localtime(mtime)
            ts_str = time.strftime(
                CACHE_SEGMENT_FORMAT.replace("%z", ""), ts_struct
            )
            tz_offset = time.strftime("%z", ts_struct)
            ts_str = f"{ts_str}{tz_offset}"

        dest_path = os.path.join(CACHE_DIR, f"{camera}@main@{ts_str}.mp4")

        # If a file with that exact timestamp already exists (clock
        # collision on subsecond-similar segments) append a counter.
        counter = 0
        while os.path.exists(dest_path):
            counter += 1
            dest_path = os.path.join(
                CACHE_DIR, f"{camera}@main@{ts_str}_{counter}.mp4"
            )
            if counter > 100:
                logger.warning(
                    f"Refusing to promote {src_path}: too many name collisions"
                )
                return

        try:
            shutil.move(src_path, dest_path)
            state.promoted_paths.add(src_path)
            logger.debug(f"Promoted main-stream buffer segment to {dest_path}")
        except OSError as e:
            logger.warning(f"Failed to promote {src_path} to {dest_path}: {e}")

    # ------------------------------ misc -------------------------------

    def _check_timeouts(self) -> None:  # pragma: no cover - retained for API compat
        """No-op kept for backwards compatibility with prior call sites."""
        return

    def _check_ffmpeg_health(self) -> None:  # pragma: no cover - retained for API compat
        """No-op: ffmpeg is owned by the camera process now."""
        return

    def _stop_all_recordings(self) -> None:  # pragma: no cover - retained for API compat
        return
