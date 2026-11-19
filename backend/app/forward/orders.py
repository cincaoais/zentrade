"""Append-only order journal and idempotent order executor.

The journal is the source of truth across crashes: ``submitting`` is written and
fsynced before the broker is called, so a key is never sent twice. An order
whose call raised is recovered from the day's order list by its ``remark``; if
it cannot be matched the key becomes ``unknown`` and no order is re-sent, but
the key is still watched: a later poll (today's list) or ``resolve_stale``
(the order history, for earlier days) records its real outcome.

Sells are capped at the shares the broker holds minus the shares this executor
has already claimed with sells that are still open, so a stale book can never
sell more than the account has.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DAY_LIMIT = 25

# Our own states. Raw SDK statuses are folded into these. A cancel that is
# still in progress is not final: fills can still arrive.
_ALIASES = {
    "filled_all": "filled",
    "filled_part": "partial",
    "cancelled_all": "cancelled",
    "cancelled_part": "cancelled",
    "cancelling_all": "submitted",
    "cancelling_part": "partial",
    "fill_cancelled": "cancelled",
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
_DONE = {"filled", "cancelled", "failed", "unknown", "skipped"}
_INFO_FIELDS = (
    "symbol",
    "side",
    "trd_side",
    "qty",
    "order_id",
    "dealt_qty",
    "dealt_avg_price",
)


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


def key_date(key: str) -> date | None:
    """The trading day encoded in ``fw-<short>-<YYYYMMDD>-...``."""
    parts = str(key).split("-")
    if len(parts) < 3 or parts[0] != "fw":
        return None
    text = parts[2]
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


class OrderCapExceeded(RuntimeError):  # noqa: N818
    """The per-strategy, per-day order cap would be exceeded."""


class Journal:
    """Append-only JSONL order journal."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, sort_keys=True, default=str)
        prefix = ""
        if self.path.exists() and self.path.stat().st_size > 0:
            with self.path.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    # A crash tore the last line: never glue a new event onto it.
                    prefix = "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(prefix + line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def events(self) -> list[dict[str, Any]]:
        """Every event; a torn final line (crash mid-write) is skipped.

        A corrupt line anywhere else is not a crash artefact and raises.
        """
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            texts = [line.strip() for line in handle if line.strip()]
        events: list[dict[str, Any]] = []
        for index, text in enumerate(texts):
            try:
                events.append(json.loads(text))
            except json.JSONDecodeError as exc:
                if index == len(texts) - 1:
                    logger.warning("%s: 忽略被截断的最后一行: %r", self.path, text[:80])
                    break
                raise ValueError(f"{self.path}: 第 {index + 1} 行损坏") from exc
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
        cap_sells: bool = True,
    ) -> None:
        self.broker = broker
        self.journal = journal
        self.clock = clock
        self.place_gap_s = float(place_gap_s)
        self.query_gap_s = float(query_gap_s)
        self.cap_sells = bool(cap_sells)
        self._last_place: float | None = None
        self._last_query: float | None = None
        self._last_positions: float | None = None
        # Sells sent by this executor that are not final yet: key -> (symbol, shares).
        self._pending: dict[str, tuple[str, int]] = {}
        # Last journalled (state, dealt_qty) per key, so unchanged polls add nothing.
        self._applied: dict[str, tuple[str, int]] = {}

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

    # ── journal-derived views ────────────────────────────────
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

    def _view(self) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
        latest: dict[str, str] = {}
        info: dict[str, dict[str, Any]] = {}
        for event in self.journal.events():
            key = event.get("key")
            if not isinstance(key, str) or not key:
                continue
            state = _state(event)
            if state is not None:
                latest[key] = state
            bag = info.setdefault(key, {})
            for field in _INFO_FIELDS:
                if event.get(field) is not None:
                    bag[field] = event[field]
        return latest, info

    @staticmethod
    def _merge(info: dict[str, dict[str, Any]], key: str, order: dict[str, Any]) -> None:
        bag = info.setdefault(key, {})
        bag.update(
            {
                "symbol": order.get("code") or order.get("symbol") or bag.get("symbol"),
                "qty": order.get("qty", bag.get("qty")),
                "order_id": order.get("order_id", bag.get("order_id")),
                "dealt_qty": order.get("dealt_qty", bag.get("dealt_qty")),
                "dealt_avg_price": order.get("dealt_avg_price", bag.get("dealt_avg_price")),
                "trd_side": order.get("trd_side", bag.get("trd_side")),
            }
        )

    def _append(self, event: dict[str, Any]) -> None:
        self.journal.append(event)
        key = str(event.get("key") or "")
        state = _state(event)
        if state is not None:
            self._applied[key] = (state, _int(event.get("dealt_qty")))
            if state in _DONE and state != "unknown":
                self._pending.pop(key, None)

    def _apply_order(self, key: str, order: dict[str, Any]) -> str:
        state = _state(order) or "unknown"
        dealt = _int(order.get("dealt_qty"))
        if self._applied.get(key) == (state, dealt):
            return state
        event: dict[str, Any] = {
            "key": key,
            "state": state,
            "ts": self.clock.monotonic(),
            "order_id": str(order.get("order_id", "")),
            "symbol": order.get("code") or order.get("symbol") or "",
            "qty": _int(order.get("qty")),
            "dealt_qty": dealt,
            "dealt_avg_price": _float(order.get("dealt_avg_price")),
            "trd_side": str(order.get("trd_side") or "").upper(),
        }
        if event["trd_side"]:
            event["side"] = _side(event["trd_side"])
        self._append(event)
        return state

    # ── sell cap ─────────────────────────────────────────────
    def _sell_allowance(self, symbol: str) -> int | None:
        """Broker shares of ``symbol`` not yet claimed by this executor's open sells."""
        if not self.cap_sells:
            return None
        reader = getattr(self.broker, "positions", None)
        if not callable(reader):
            return None
        same = [key for key, (sym, _) in self._pending.items() if sym == symbol]
        if same:
            # A claimed sell may have filled since: drop it before counting.
            self.refresh(keys=same)
        self._last_positions = self._pace(self._last_positions, self.query_gap_s)
        held = _int((reader() or {}).get(symbol, 0))
        claimed = sum(qty for sym, qty in self._pending.values() if sym == symbol)
        return held - claimed

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

        capped_from: int | None = None
        if side_code == "S":
            allowed = self._sell_allowance(symbol)
            if allowed is not None and allowed <= 0:
                self._append(
                    {
                        "key": key,
                        "state": "skipped",
                        "ts": self.clock.monotonic(),
                        "symbol": symbol,
                        "side": side_code,
                        "qty": shares,
                        "reason": "broker holds no unclaimed shares",
                    }
                )
                return "skipped"
            if allowed is not None and allowed < shares:
                capped_from, shares = shares, allowed

        submitting: dict[str, Any] = {
            "key": key,
            "state": "submitting",
            "ts": self.clock.monotonic(),
            "symbol": symbol,
            "side": side_code,
            "qty": shares,
        }
        if capped_from is not None:
            submitting["capped_from"] = capped_from
        self._append(submitting)
        if side_code == "S":
            self._pending[key] = (symbol, shares)
        self._place_time()
        try:
            order_id = self.broker.place_market(symbol, side_code, shares, remark=key)
        except Exception:
            # The broker may have accepted before the failure (OpenD restart).
            return self.resolve(key)

        self._append(
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
            self._append({"key": key, "state": "unknown", "ts": self.clock.monotonic()})
            return "unknown"
        order = next(
            (row for row in orders if str(row.get("remark", "")) == key),
            None,
        )
        if order is None:
            self._append({"key": key, "state": "unknown", "ts": self.clock.monotonic()})
            return "unknown"
        return self._apply_order(key, order)

    def refresh(self, keys: list[str] | None = None) -> dict[str, str]:
        """One read of today's orders for every active key; never cancels."""
        latest, _info = self._view()
        targets = [
            key
            for key, state in latest.items()
            if state in _ACTIVE and (keys is None or key in keys)
        ]
        if not targets:
            return {}
        orders = self._query_orders()
        by_remark = {str(row.get("remark", "")): row for row in orders}
        result: dict[str, str] = {}
        for key in targets:
            order = by_remark.get(key)
            result[key] = latest[key] if order is None else self._apply_order(key, order)
        return result

    def resolve_stale(self, today: date) -> dict[str, str]:
        """Settle active keys of trading days before ``today`` from the order history.

        ``order_list_query`` lists only today's orders, so a key left open by a
        crash, a late restart or an unpolled stop is read from
        ``history_order_list_query``. A DAY order of an earlier day cannot still
        be working, so an open status there is recorded as cancelled with its
        dealt quantity. A key missing from the history stays ``unknown``.
        """
        latest, info = self._view()
        stale: dict[str, date] = {}
        for key, state in latest.items():
            day = key_date(key)
            if state in _ACTIVE and day is not None and day < today:
                stale[key] = day
        if not stale:
            return {}
        reader = getattr(self.broker, "history_orders", None)
        if not callable(reader):
            return {key: latest[key] for key in stale}
        self._last_query = self._pace(self._last_query, self.query_gap_s)
        rows = reader(min(stale.values()), max(stale.values()))
        by_remark = {str(row.get("remark", "")): row for row in rows}
        result: dict[str, str] = {}
        for key in sorted(stale):
            order = by_remark.get(key)
            if order is None:
                if latest[key] != "unknown":
                    self._append(
                        {
                            "key": key,
                            "state": "unknown",
                            "ts": self.clock.monotonic(),
                            "reason": "not in order history",
                        }
                    )
                result[key] = "unknown"
                continue
            state = self._apply_order(key, order)
            if state in _OPEN:
                self._merge(info, key, order)
                bag = info[key]
                self._append(
                    {
                        "key": key,
                        "state": "cancelled",
                        "ts": self.clock.monotonic(),
                        "reason": "day order of an earlier day",
                        "order_id": str(bag.get("order_id") or ""),
                        "symbol": bag.get("symbol", ""),
                        "side": _side(bag.get("side") or bag.get("trd_side")),
                        "qty": _int(bag.get("qty")),
                        "dealt_qty": _int(bag.get("dealt_qty")),
                        "dealt_avg_price": _float(bag.get("dealt_avg_price")),
                    }
                )
                state = "cancelled"
            result[key] = state
        return result

    # ── polling ──────────────────────────────────────────────
    def poll_until(self, deadline: float) -> dict[str, dict[str, Any]]:
        """Poll until every open order is final or ``deadline``.

        ``unknown`` keys are watched too (their order may appear late) but do
        not hold the loop. Open orders are cancelled at the deadline and then
        read again, so a fill that landed just before the cancel is recorded. A
        cancelled, partially filled SELL gets exactly one remainder order
        (``<key>-r1``) for the unfilled shares.
        """
        latest, info = self._view()
        open_keys = [key for key, state in latest.items() if state in _OPEN]
        unknown_keys = [key for key, state in latest.items() if state == "unknown"]
        if not open_keys and not unknown_keys:
            return {}

        deadline = float(deadline)
        seen: set[str] = set()
        while True:
            orders = self._query_orders()
            by_remark = {str(row.get("remark", "")): row for row in orders}
            for key in [*open_keys, *unknown_keys]:
                if latest.get(key) not in _ACTIVE:
                    continue
                order = by_remark.get(key)
                if order is None:
                    # Possibly a stale list: keep the order live until the
                    # deadline so it can still be cancelled; unknown only then.
                    continue
                seen.add(key)
                latest[key] = self._apply_order(key, order)
                self._merge(info, key, order)
            if all(latest.get(key) not in _OPEN for key in open_keys):
                break
            now = self.clock.monotonic()
            if now >= deadline:
                break
            self.clock.sleep(min(1.0, deadline - now))

        # Never seen in the order list by the deadline: its fills cannot be known
        # now. Cancel it by id if we have one (best effort) and record unknown;
        # it stays watched and is never re-sent.
        for key in open_keys:
            if key in seen or latest.get(key) not in _OPEN:
                continue
            order_id = str(info.get(key, {}).get("order_id") or "")
            if order_id:
                with contextlib.suppress(Exception):
                    self.broker.cancel(order_id)
            latest[key] = "unknown"
            self._append(
                {"key": key, "state": "unknown", "ts": self.clock.monotonic(), "order_id": order_id}
            )

        # Cancel anything still open at the deadline, then read the list again.
        cancel_failed: dict[str, str] = {}
        cancelled: list[str] = []
        for key in open_keys:
            if latest.get(key) not in {"submitted", "partial"}:
                continue
            order_id = str(info.get(key, {}).get("order_id") or "")
            if not order_id:
                continue
            try:
                self.broker.cancel(order_id)
            except Exception as exc:
                cancel_failed[key] = f"{type(exc).__name__}: {exc}"
                continue
            cancelled.append(key)

        if cancelled or cancel_failed:
            try:
                after = {
                    str(row.get("remark", "")): row for row in self._query_orders()
                }
            except Exception:
                after = {}
            for key in [*cancelled, *cancel_failed]:
                order = after.get(key)
                if order is not None:
                    self._merge(info, key, order)
                    if _state(order) in {"filled", "cancelled", "failed"}:
                        latest[key] = self._apply_order(key, order)
                        continue
                bag = info.setdefault(key, {})
                event = {
                    "key": key,
                    "ts": self.clock.monotonic(),
                    "order_id": str(bag.get("order_id") or ""),
                    "symbol": bag.get("symbol", ""),
                    "side": _side(bag.get("side") or bag.get("trd_side")),
                    "qty": _int(bag.get("qty")),
                    "dealt_qty": _int(bag.get("dealt_qty")),
                    "dealt_avg_price": _float(bag.get("dealt_avg_price")),
                }
                if key in cancel_failed:
                    # Neither the cancel nor the list settled it: keep what is
                    # known (the dealt quantity so far) and watch it.
                    latest[key] = "unknown"
                    self._append({**event, "state": "unknown", "error": cancel_failed[key]})
                else:
                    latest[key] = "cancelled"
                    self._append({**event, "state": "cancelled"})

        # One retry for the unfilled remainder of a cancelled partial exit.
        for key in open_keys:
            if key.endswith("-r1") or latest.get(key) != "cancelled":
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

        result_keys = [
            *open_keys,
            *unknown_keys,
            *(key for key in latest if key.endswith("-r1") and key not in open_keys),
        ]
        return {
            key: {
                "status": latest.get(key, "unknown"),
                "dealt_qty": _int(info.get(key, {}).get("dealt_qty")),
                "dealt_avg_price": _float(info.get(key, {}).get("dealt_avg_price")),
                "order_id": str(info.get(key, {}).get("order_id") or ""),
            }
            for key in dict.fromkeys(result_keys)
        }


__all__ = ["DAY_LIMIT", "Executor", "Journal", "OrderCapExceeded", "key_date"]
