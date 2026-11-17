# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""The failed sign-in limiter: counting, the window, clearing, and memory."""

import unittest
from unittest.mock import patch

from labrat.auth import throttle as module
from labrat.auth.throttle import SignInThrottle, throttle_keys


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Read the clock.

        Returns:
            float: The current fake time.
        """
        return self.now


class SignInThrottleTests(unittest.TestCase):
    """Check the limiter against a hand-moved clock."""

    def setUp(self) -> None:
        """Make a limiter of three failures per minute."""
        self.clock = FakeClock()
        self.throttle = SignInThrottle(limit=3, window=60, clock=self.clock)
        self.keys = throttle_keys("admin", "192.0.2.5")

    def test_keys_cover_username_and_address(self):
        self.assertEqual(self.keys, ["user:admin", "addr:192.0.2.5"])
        self.assertEqual(throttle_keys("admin", None), ["user:admin"])

    def test_blocks_at_the_limit_and_reports_the_trip_once(self):
        self.assertEqual([self.throttle.record_failure(self.keys) for _ in range(3)], [False, False, True])
        self.assertTrue(self.throttle.blocked(self.keys))
        self.assertFalse(self.throttle.record_failure(self.keys))

    def test_another_address_is_still_blocked_by_username(self):
        for _ in range(3):
            self.throttle.record_failure(self.keys)
        self.assertTrue(self.throttle.blocked(throttle_keys("admin", "198.51.100.7")))
        self.assertFalse(self.throttle.blocked(throttle_keys("someone", "198.51.100.7")))

    def test_failures_age_out_of_the_window(self):
        for _ in range(3):
            self.throttle.record_failure(self.keys)
        self.clock.now = 61
        self.assertFalse(self.throttle.blocked(self.keys))

    def test_success_clears_the_count(self):
        for _ in range(2):
            self.throttle.record_failure(self.keys)
        self.throttle.clear(self.keys)
        self.throttle.record_failure(self.keys)
        self.assertFalse(self.throttle.blocked(self.keys))

    def test_memory_is_bounded(self):
        with patch.object(module, "MAX_TRACKED", 4):
            for index in range(10):
                self.throttle.record_failure([f"user:u{index}"])
        self.assertEqual(len(self.throttle._failures), 4)


if __name__ == "__main__":
    unittest.main()
