# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Slow down repeated failed sign-ins.

Failures are counted per username and per client address in a sliding
window. Once either count reaches the limit, sign-in for that username or
address is refused without checking the password until old failures age
out. A successful sign-in clears that username's and address's counts.

Counts live in memory, per web worker process, so a restart forgets them
and each worker counts on its own; that is enough to make guessing slow
without a shared store. Behind a reverse proxy every client shares the
proxy's address, so the per-username count does most of the work.

Used by: app, pages.private, pages.public.
"""

from collections import OrderedDict
import threading
import time

#: Failed sign-ins allowed per username or address within the window.
FAILURE_LIMIT = 10
#: Seconds a failure counts against its username and address.
WINDOW_SECONDS = 15 * 60
#: Most usernames and addresses tracked at once; the oldest go first.
MAX_TRACKED = 10_000


class SignInThrottle:
    """Count recent sign-in failures per key and refuse keys over the limit."""

    def __init__(self, limit: int = FAILURE_LIMIT, window: float = WINDOW_SECONDS,
                 clock=time.monotonic) -> None:
        """Start with no recorded failures.

        Args:
            limit: Failures allowed within the window.
            window: Seconds a failure counts.
            clock: Monotonic time source (replaceable in tests).
        """
        self.limit = limit
        self.window = window
        self.clock = clock
        self._failures: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        """Drop a key's expired failures and return the rest.

        Args:
            key: A ``user:`` or ``addr:`` key.
            now: The current clock reading.

        Returns:
            list[float]: The key's failure times still inside the window.
        """
        times = [stamp for stamp in self._failures.get(key, []) if now - stamp < self.window]
        if times:
            self._failures[key] = times
        else:
            self._failures.pop(key, None)
        return times

    def blocked(self, keys: list[str]) -> bool:
        """Say whether any of these keys has reached the limit.

        Args:
            keys: The sign-in's username and address keys.

        Returns:
            bool: True when sign-in must be refused for now.
        """
        with self._lock:
            now = self.clock()
            return any(len(self._recent(key, now)) >= self.limit for key in keys)

    def record_failure(self, keys: list[str]) -> bool:
        """Count a failed sign-in against each key.

        Args:
            keys: The sign-in's username and address keys.

        Returns:
            bool: True when this failure brought a key to the limit.
        """
        with self._lock:
            now = self.clock()
            tripped = False
            for key in keys:
                times = self._recent(key, now)
                times.append(now)
                self._failures[key] = times
                self._failures.move_to_end(key)
                tripped = tripped or len(times) == self.limit
            while len(self._failures) > MAX_TRACKED:
                self._failures.popitem(last=False)
            return tripped

    def clear(self, keys: list[str]) -> None:
        """Forget the failures of a sign-in that succeeded.

        Args:
            keys: The sign-in's username and address keys.
        """
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)


def throttle_keys(username: str, address: str | None) -> list[str]:
    """Build the keys a sign-in counts against.

    Args:
        username: The normalized username submitted.
        address: The client address, when known.

    Returns:
        list[str]: One key per username and address.
    """
    keys = [f"user:{username}"]
    if address:
        keys.append(f"addr:{address}")
    return keys
