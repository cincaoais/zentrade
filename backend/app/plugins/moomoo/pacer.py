"""Sliding-window call pacer for moomoo OpenD rate limits.

OpenD limits some quote queries per rolling window (e.g. ``get_market_snapshot``
and ``get_rehab`` at 60 calls / 30 s). ``Pacer`` blocks callers so that no more
than ``max_calls`` calls return within any ``window_s`` seconds. It is
thread-safe: the check, sleep and record happen under one lock, so concurrent
callers serialize instead of racing past the limit.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class Pacer:
    """Limit calls to ``max_calls`` per rolling ``window_s`` seconds."""

    def __init__(
        self,
        max_calls: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_calls < 1:
            raise ValueError(f"max_calls must be >= 1, got {max_calls!r}")
        if window_s <= 0:
            raise ValueError(f"window_s must be > 0, got {window_s!r}")
        self._max_calls = int(max_calls)
        self._window_s = float(window_s)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._returns: deque[float] = deque()

    def wait(self) -> None:
        """Block until one more call is allowed, then record it."""
        with self._lock:
            while True:
                now = self._clock()
                cutoff = now - self._window_s
                while self._returns and self._returns[0] <= cutoff:
                    self._returns.popleft()
                if len(self._returns) < self._max_calls:
                    self._returns.append(self._clock())
                    return
                delay = self._returns[0] + self._window_s - now
                if delay > 0:
                    self._sleep(delay)