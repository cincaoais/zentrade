"""Tests for the moomoo sliding-window call pacer."""

from __future__ import annotations

import threading
from bisect import bisect_left

from app.plugins.moomoo.pacer import Pacer


class FakeClock:
    """Deterministic clock whose ``sleep`` advances time instead of blocking."""

    def __init__(self) -> None:
        self._now = 0.0
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            if seconds < 0:
                raise ValueError("negative sleep")
            self._now += seconds


def _max_calls_in_window(timestamps: list[float], window_s: float) -> int:
    """Largest number of timestamps inside any half-open ``[t, t + window_s)`` window."""
    ordered = sorted(timestamps)
    best = 0
    for start in ordered:
        count = bisect_left(ordered, start + window_s) - bisect_left(ordered, start)
        best = max(best, count)
    return best


def test_pacer_allows_burst_then_waits() -> None:
    clock = FakeClock()
    pacer = Pacer(max_calls=60, window_s=30.0, clock=clock.now, sleep=clock.sleep)

    returns: list[float] = []
    for _ in range(60):
        pacer.wait()
        returns.append(clock.now())

    # The first 60 calls are a free burst: no sleeping.
    assert returns == [0.0] * 60

    # The 61st call must wait until the oldest return leaves the window.
    pacer.wait()
    returns.append(clock.now())
    assert clock.now() >= 30.0
    assert clock.now() == 30.0

    # 59 more calls are free again after the window slid.
    for _ in range(59):
        pacer.wait()
        returns.append(clock.now())
    assert clock.now() == 30.0

    # Never more than 60 returns in any 30 s window.
    assert _max_calls_in_window(returns, 30.0) <= 60
    assert _max_calls_in_window(returns, 30.0) == 60


def test_pacer_is_thread_safe() -> None:
    clock = FakeClock()
    pacer = Pacer(max_calls=5, window_s=1.0, clock=clock.now, sleep=clock.sleep)
    returns: list[float] = []
    returns_lock = threading.Lock()

    def worker() -> None:
        for _ in range(8):
            pacer.wait()
            with returns_lock:
                returns.append(clock.now())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert len(returns) == 40
    assert _max_calls_in_window(returns, 1.0) <= 5