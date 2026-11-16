"""No-network moomoo SDK doubles for tests.

``FakeQuoteContext`` mirrors the small part of ``moomoo.OpenQuoteContext`` that
the plugin uses: every call is recorded with the ``FakeClock`` timestamp, and
responses/errors are injectable per method. ``FakeClock`` lets pacing tests move
time deterministically without sleeping.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FakeClock:
    """Deterministic monotonic clock; ``sleep`` advances instead of blocking."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += float(seconds)

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


class FakeQuoteContext:
    """Records calls; returns configurable values or raises configurable errors."""

    _DEFAULTS: dict[str, Any] = {
        "get_global_state": (0, {"qot_logined": True, "market_us": "AFTER_HOURS_END"}),
        "query_subscription": (0, []),
        "subscribe": (0, []),
        "unsubscribe": (0, None),
        "get_cur_kline": (0, None),
        "get_stock_basicinfo": (0, []),
        "get_rehab": (0, []),
        "get_market_snapshot": (0, []),
    }

    def __init__(
        self,
        clock: FakeClock | None = None,
        *,
        returns: dict[str, Any] | None = None,
        errors: dict[str, BaseException] | None = None,
    ) -> None:
        self.clock = clock or FakeClock()
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.returns: dict[str, Any] = dict(self._DEFAULTS)
        self.returns.update(returns or {})
        self.errors: dict[str, BaseException] = dict(errors or {})

    # ── call plumbing ────────────────────────────────────────
    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(
            {"name": name, "args": args, "kwargs": kwargs, "time": self.clock.monotonic()}
        )
        error = self.errors.get(name)
        if error is not None:
            raise error
        response = self.returns.get(name)
        return response(*args, **kwargs) if callable(response) else response

    def calls_named(self, name: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["name"] == name]

    def close(self) -> None:
        self.closed = True
        self.calls.append({"name": "close", "args": (), "kwargs": {}, "time": self.clock.monotonic()})

    # ── quote-context surface used by the plugin ─────────────
    def get_global_state(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("get_global_state", *args, **kwargs)

    def query_subscription(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("query_subscription", *args, **kwargs)

    def subscribe(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("subscribe", *args, **kwargs)

    def unsubscribe(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("unsubscribe", *args, **kwargs)

    def get_cur_kline(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("get_cur_kline", *args, **kwargs)

    def get_stock_basicinfo(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("get_stock_basicinfo", *args, **kwargs)

    def get_rehab(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("get_rehab", *args, **kwargs)

    def get_market_snapshot(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("get_market_snapshot", *args, **kwargs)


CtxFactory = Callable[[str, int], Any]