"""Rate-limit recurring recording cache warnings."""

import time
from collections.abc import Callable
from typing import Hashable


class WarningRateLimiter:
    """Allow the first warning for a key and one per interval afterward."""

    def __init__(
        self,
        *,
        interval: float,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._interval = interval
        self._clock = clock or (lambda: time.monotonic())
        self._last_logged: dict[Hashable, float] = {}

    def should_log(self, key: Hashable) -> bool:
        """Return whether the caller should emit a warning for this key."""
        now = self._clock()
        last_logged = self._last_logged.get(key)
        if last_logged is not None and now - last_logged < self._interval:
            return False

        self._last_logged[key] = now
        return True
