"""Append-only order journal and idempotent order executor.

The journal is the source of truth across crashes: ``submitting`` is written and
fsynced before the broker is called, so a key is never sent twice. An order
whose call raised is recovered from the day's order list by its ``remark``; if
it cannot be matched the key becomes ``unknown`` and no order is re-sent.
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

DAY_LIMIT = 25

# Our own states. Raw SDK statuses are folded into these.
_ALIASES = {
    "filled_all": "filled",
    "filled_part": "partial",
    "cancelled_all": "cancelled",
    "cancelled_part": "cancelled",
    "cancelling_all": "cancelled",
    "cancelling_part": "cancelled",
    "deleted": "cancelled",
    "disabled": "cancelled",
    "failed": "failed",
    "submit_failed": "failed",
    "timeout": "failed",
    "submitted": "submitted",
    "waiting_submit": "submitted",
    "unsubmitted": "submitted",
    "submitting": "submitting",
}

_ACTIVE = {"submitting", "submitted", "partial", "unknown"}
_OPEN = {"submitting", "submitted", "partial"}
_DONE = {"filled", "cancelled", "failed", "unknown"}


def _state(event: dict[str, Any]) -> str | None:
    raw = event.get("state")
    if raw is None:
        raw = event.get("status")
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return _ALIASES.get(text, text)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _side(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"B", "BUY", "BUY_BACK"}:
        return "B"
    if text in {"S", "SELL", "SELL_SHORT"}:
        return "S"
    return text


class OrderCapExceeded(RuntimeError):  # noqa: N818
    """The per-strategy, per-day order cap would be exceeded."""


class Journal:
    """Append-only JSONL order journal."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, sort_keys=True, default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if text:
                    events.append(json.loads(text))
        return events

    def state(self, key: str) -> str | None:
        latest: str | None = None
        for event in self.events():
            if event.get("key") == key:
                state = _state(event)
                if state is not None:
                    latest = state
        return latest


class Executor:
    """Paced, idempotent submission over ``SimBroker``."""

    def __init__(
        self,
        broker: Any,
        journal: Journal,
        *,
        clock: Any,
        place_gap_s: float = 2.5,
        query_gap_s: float = 3.5,
    ) -> None:
        self.broker = broker
        self.journal = journal
        self.clock = clock
        self.place_gap_s = float(place_gap_s)
        self.query_gap_s = float(query_gap_s)
        self._last_place: float | None = None
        self._last_query: float | None = None

    # ── pacing ───────────────────────────────────────────────
    def _pace(self, last: float | None, gap: float) -> float:
        now = self.clock.monotonic()
        if last is not None:
            remaining = gap - (now - last)
            if remaining > 0:
                self.clock.sleep(remaining)
                now = self.clock.monotonic()
        return now

    def _place_time(self) -> None:
        self._last_place = self._pace(self._last_place, self.place_gap_s)

    def _query_orders(self) -> list[dict[str, Any]]:
        self._last_query = self._pace(self._last_query, self.query_gap_s)
        return self.broker.orders_today()

    # ── journal-derived counters ─────────────────────────────
    def daily_count(self, short: str, day: date | str) -> int:
        day_text = day.strftime("%Y%m%d") if isinstance(day, date) else str(day)
        prefix = f"fw-{short}-{day_text}-"
        keys = {
            str(event.get("key"))
            for event in self.journal.events()
            if _state(event) == "submitting"
            and str(event.get("key", "")).startswith(prefix)
        }
        return len(keys)

    def _apply_order(self, key: str, order: dict[str, Any]) -> str:
        state = _state(order) or "unknown"
        event: dict[str, Any] = {
            "key": key,
            "state": state,
            "ts": self.clock.monotonic(),
            "order_id": str(order.get("order_id", "")),
            "symbol": order.get("code") or order.get("symbol") or "",
            "qty": _int(order.get("qty")),
            "dealt_qty": _int(order.get("dealt_qty")),
            "dealt_avg_price": _float(order.get("dealt_avg_price")),
            "trd_side": str(order.get("trd_side") or "").upper(),
        }
        if event["trd_side"]:
            event["side"] = _side(event["trd_side"])
        self.journal.append(event)
        return state

    # ── submission and recovery ──────────────────────────────
    def submit(self, key: str, symbol: str, side: str, qty: int) -> str:
        shares = int(qty)
        if shares <= 0:
            raise ValueError(f"数量必须为正: {qty!r}")
        side_code = _side(side)
        if side_code not in {"B", "S"}:
            raise ValueError(f"未知交易方向: {side!r}")

        existing = self.journal.state(key)
        if existing is not None:
            if existing in _ACTIVE:
                return self.resolve(key)
            return existing

        parts = key.split("-")
        if (
            len(parts) >= 3
            and parts[0] == "fw"
            and self.daily_count(parts[1], parts[2]) >= DAY_LIMIT
        ):
            raise OrderCapExceeded(f"{parts[1]} 在 {parts[2]} 已到 {DAY_LIMIT} 单上限")

        self.journal.append(
            {
                "key": key,
                "state": "submitting",
                "ts": self.clock.monotonic(),
                "symbol": symbol,
                "side": side_code,
                "qty": shares,
            }
        )
        self._place_time()
        try:
            order_id = self.broker.place_market(symbol, side_code, shares, remark=key)
        except Exception:
            # The broker may have accepted before the failure (OpenD restart).
            return self.resolve(key)

        self.journal.append(
            {
                "key": key,
                "state": "submitted",
                "ts": self.clock.monotonic(),
                "order_id": str(order_id),
                "symbol": symbol,
                "side": side_code,
                "qty": shares,
            }
        )
        return str(order_id)

    def resolve(self, key: str) -> str:
        try:
            orders = self._query_orders()
        except Exception:
            self.journal.append(
                {"key": key, "state": "unknown", "ts": self.clock.monotonic()}
            )
            return "unknown"
        order = next(
            (row for row in orders if str(row.get("remark", "")) == key),
            None,
        )
        if order is None:
            self.journal.append(
                {"key": key, "state": "unknown", "ts": self.clock.monotonic()}
            )
            return "unknown"
        return self._apply_order(key, order)

    # ── polling ──────────────────────────────────────────────
    def poll_until(self, deadline: float) -> dict[str, dict[str, Any]]:
        """Poll until every tracked order is final or ``deadline``.

        Open orders are cancelled at the deadline. A partially filled SELL then
        gets exactly one remainder order (``<key>-r1``) for the unfilled shares.
        """
        events = self.journal.events()
        latest: dict[str, str] = {}
        info: dict[str, dict[str, Any]] = {}
        for event in events:
            key = event.get("key")
            if not isinstance(key, str) or not key:
                continue
            state = _state(event)
            if state is not None:
                latest[key] = state
            bag = info.setdefault(key, {})
            for field in (
                "symbol",
                "side",
                "trd_side",
                "qty",
                "order_id",
                "dealt_qty",
                "dealt_avg_price",
            ):
                if event.get(field) is not None:
                    bag[field] = event[field]

        active_keys = [key for key, state in latest.items() if state in _OPEN]
        if not active_keys:
            return {}

        deadline = float(deadline)
        seen: set[str] = set()
        while True:
            orders = self._query_orders()
            by_remark = {str(row.get("remark", "")): row for row in orders}
            for key in active_keys:
                if latest.get(key) not in _OPEN:
                    continue
                order = by_remark.get(key)
                if order is None:
                    # Possibly a stale list: keep the order live until the
                    # deadline so it can still be cancelled; unknown only then.
                    continue
                seen.add(key)
                latest[key] = self._apply_order(key, order)
                bag = info.setdefault(key, {})
                bag.update(
                    {
                        "symbol": order.get("code") or order.get("symbol") or bag.get("symbol"),
                        "qty": order.get("qty", bag.get("qty")),
                        "order_id": order.get("order_id", bag.get("order_id")),
                        "dealt_qty": order.get("dealt_qty", bag.get("dealt_qty")),
                        "dealt_avg_price": order.get(
                            "dealt_avg_price", bag.get("dealt_avg_price")
                        ),
                        "trd_side": order.get("trd_side", bag.get("trd_side")),
                    }
                )
            if all(latest.get(key) in _DONE for key in active_keys):
                break
            now = self.clock.monotonic()
            if now >= deadline:
                break
            self.clock.sleep(min(1.0, deadline - now))

        # Never seen in the order list by the deadline: its fills cannot be known.
        # Cancel it by id if we have one (best effort), record unknown, never re-send.
        for key in active_keys:
            if key in seen or latest.get(key) not in _OPEN:
                continue
            order_id = str(info.get(key, {}).get("order_id") or "")
            if order_id:
                with contextlib.suppress(Exception):
                    self.broker.cancel(order_id)
            latest[key] = "unknown"
            self.journal.append(
                {"key": key, "state": "unknown", "ts": self.clock.monotonic(), "order_id": order_id}
            )

        # Cancel anything still open at the deadline.
        for key in active_keys:
            state = latest.get(key)
            if state not in {"submitted", "partial"}:
                continue
            order_id = str(info.get(key, {}).get("order_id") or "")
            if not order_id:
                continue
            try:
                self.broker.cancel(order_id)
            except Exception:
                latest[key] = "unknown"
                self.journal.append(
                    {"key": key, "state": "unknown", "ts": self.clock.monotonic()}
                )
                continue
            latest[key] = "cancelled"
            bag = info.setdefault(key, {})
            self.journal.append(
                {
                    "key": key,
                    "state": "cancelled",
                    "ts": self.clock.monotonic(),
                    "order_id": order_id,
                    "symbol": bag.get("symbol", ""),
                    "side": _side(bag.get("side") or bag.get("trd_side")),
                    "qty": _int(bag.get("qty")),
                    "dealt_qty": _int(bag.get("dealt_qty")),
                    "dealt_avg_price": _float(bag.get("dealt_avg_price")),
                }
            )

        # One retry for the unfilled remainder of a partial exit.
        for key in active_keys:
            if key.endswith("-r1"):
                continue
            bag = info.get(key, {})
            side = _side(bag.get("side") or bag.get("trd_side"))
            qty = _int(bag.get("qty"))
            dealt = _int(bag.get("dealt_qty"))
            if side != "S" or dealt <= 0 or dealt >= qty:
                continue
            retry = f"{key}-r1"
            if self.journal.state(retry) is not None:
                continue
            symbol = str(bag.get("symbol") or "")
            if not symbol:
                continue
            try:
                self.submit(retry, symbol, "S", qty - dealt)
            except OrderCapExceeded:
                latest[retry] = "failed"
                continue
            latest[retry] = self.journal.state(retry) or "submitted"
            info[retry] = {
                "symbol": symbol,
                "side": "S",
                "qty": qty - dealt,
                "dealt_qty": 0,
            }

        result_keys = [*active_keys, *(key for key in latest if key.endswith("-r1") and key not in active_keys)]
        return {
            key: {
                "status": latest.get(key, "unknown"),
                "dealt_qty": _int(info.get(key, {}).get("dealt_qty")),
                "dealt_avg_price": _float(info.get(key, {}).get("dealt_avg_price")),
                "order_id": str(info.get(key, {}).get("order_id") or ""),
            }
            for key in result_keys
        }


__all__ = ["DAY_LIMIT", "Executor", "Journal", "OrderCapExceeded"]