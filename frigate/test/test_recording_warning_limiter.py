"""Tests for recording cache warning rate limiting."""

import unittest

from frigate.record.warning_limiter import WarningRateLimiter


class TestRecordingWarningRateLimiter(unittest.TestCase):
    def test_repeated_unprocessed_segment_warning_is_rate_limited(self) -> None:
        now = [100.0]
        limiter = WarningRateLimiter(interval=300.0, clock=lambda: now[0])

        self.assertTrue(limiter.should_log(("Front_Gate", "unprocessed")))
        self.assertFalse(limiter.should_log(("Front_Gate", "unprocessed")))

        now[0] += 301.0
        self.assertTrue(limiter.should_log(("Front_Gate", "unprocessed")))

    def test_warning_types_and_cameras_have_independent_limits(self) -> None:
        limiter = WarningRateLimiter(interval=300.0, clock=lambda: 100.0)

        self.assertTrue(limiter.should_log(("Front_Gate", "unprocessed")))
        self.assertTrue(limiter.should_log(("Front_Gate", "processed")))
        self.assertTrue(limiter.should_log(("Front_Driveway", "unprocessed")))


if __name__ == "__main__":
    unittest.main()
