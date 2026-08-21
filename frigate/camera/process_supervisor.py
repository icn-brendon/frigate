"""Supervise per-camera tracking and capture processes."""

import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Protocol

logger = logging.getLogger(__name__)

CAMERA_PROCESS_MAX_RESTARTS = 5
CAMERA_PROCESS_RESTART_WINDOW = 60.0
CAMERA_PROCESS_STALE_AFTER = 60.0


class ProcessLike(Protocol):
    pid: int | None
    exitcode: int | None

    def is_alive(self) -> bool: ...


class CameraProcessSupervisor:
    """Detect failed or stalled camera workers and request bounded recovery."""

    def __init__(
        self,
        restart_camera: Callable[[str], None],
        *,
        max_restarts: int = CAMERA_PROCESS_MAX_RESTARTS,
        restart_window: float = CAMERA_PROCESS_RESTART_WINDOW,
        stale_after: float = CAMERA_PROCESS_STALE_AFTER,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._restart_camera = restart_camera
        self._max_restarts = max_restarts
        self._restart_window = restart_window
        self._stale_after = stale_after
        self._clock = clock or time.monotonic
        self._restart_timestamps: dict[str, deque[float]] = defaultdict(deque)
        self._last_detection_frame: dict[str, tuple[float, float]] = {}
        self._backoff_logged: set[str] = set()

    def forget(self, camera: str) -> None:
        """Drop supervision state for a disabled or removed camera."""
        self._restart_timestamps.pop(camera, None)
        self._last_detection_frame.pop(camera, None)
        self._backoff_logged.discard(camera)

    def check(
        self,
        camera: str,
        tracker: ProcessLike | None,
        capture: ProcessLike | None,
        *,
        detection_frame: float,
    ) -> bool:
        """Check one camera and recover its process pair when unhealthy."""
        now = self._clock()
        failures = self._process_failures(tracker, capture)

        if failures:
            reason = ", ".join(failures)
        else:
            previous = self._last_detection_frame.get(camera)
            if previous is None or detection_frame != previous[0]:
                self._last_detection_frame[camera] = (detection_frame, now)
                self._backoff_logged.discard(camera)
                return False

            stalled_for = now - previous[1]
            if stalled_for <= self._stale_after:
                return False

            reason = (
                f"detection frame stalled at {detection_frame:.3f} "
                f"for {stalled_for:.0f}s"
            )

        timestamps = self._restart_timestamps[camera]
        while timestamps and now - timestamps[0] > self._restart_window:
            timestamps.popleft()

        if len(timestamps) >= self._max_restarts:
            if camera not in self._backoff_logged:
                logger.error(
                    "Camera %s processes restarting too frequently "
                    "(%d times in %.0fs), backing off",
                    camera,
                    self._max_restarts,
                    self._restart_window,
                )
                self._backoff_logged.add(camera)
            return False

        logger.warning("Camera %s process failure detected: %s", camera, reason)
        timestamps.append(now)
        self._backoff_logged.discard(camera)

        try:
            self._restart_camera(camera)
        except Exception:
            logger.exception("Failed to restart camera processes for %s", camera)
            return False

        self._last_detection_frame.pop(camera, None)
        logger.info("Restarted camera process pair for %s", camera)
        return True

    @staticmethod
    def _process_failures(
        tracker: ProcessLike | None, capture: ProcessLike | None
    ) -> list[str]:
        failures = []
        for name, process in (("tracker", tracker), ("capture", capture)):
            if process is None:
                failures.append(f"{name} missing")
            elif not process.is_alive():
                failures.append(
                    f"{name} pid={process.pid} exitcode={process.exitcode}"
                )
        return failures
