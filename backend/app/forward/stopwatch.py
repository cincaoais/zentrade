"""Intraday -15% stop watch (spec S1).

For every live lot, when the last trade at or below ``entry_price * 0.85`` the
watch sends one market sell for that lot. The line uses the **engine's** entry
price, not the live fill price, so the live stop and the research stop sit on
the same line (spec 6.5). Engine entry prices are supplied by the caller from
the saved decision records; a lot without one is never sold on a guessed line:
it is skipped and reported with ``reason="no_engine_entry_price"``.

Quotes older than ``STALE_QUOTE_S`` are ignored and logged. Each lot is sold at
most once per trading day (the order key is deterministic and the executor is
idempotent as well).
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from app.forward.book import LiveBook

logger = logging.getLogger(__name__)

STALE_QUOTE_S = 60.0
STOP_FRACTION = 0.85


def stop_line(entry_price: float) -> float:
    """The engine's -15% stop price for ``entry_price``."""
    price = float(entry_price)
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"入场价必须为正的有限数: {entry_price!r}")
    return price * STOP_FRACTION


def _epoch(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _last_price(snapshot: Mapping[str, Any] | None) -> float | None:
    if not snapshot:
        return None
    raw = snapshot.get("last_price")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


class StopWatch:
    """Polls snapshots and sends at most one stop sell per lot per day."""

    def __init__(
        self,
        books: Iterable[LiveBook] | Mapping[str, LiveBook],
        quotes: Any,
        executor: Any,
        engine_entry_prices: dict[str, dict[str, float]],
        *,
        clock: Any,
        poll_s: float = 5.0,
        dry_run: bool = False,
    ) -> None:
        if isinstance(books, Mapping):
            self.books: list[LiveBook] = list(books.values())
        else:
            self.books = list(books)
        self.quotes = quotes
        self.executor = executor
        self.engine_entry_prices = {
            str(short): {str(sym): float(price) for sym, price in prices.items()}
            for short, prices in (engine_entry_prices or {}).items()
        }
        self.clock = clock
        self.poll_s = float(poll_s)
        self.dry_run = bool(dry_run)
        self._sold: dict[date, set[str]] = {}
        self._skipped: dict[date, set[tuple[str, str]]] = {}

    # ── per-tick decision ────────────────────────────────────
    def tick(self, now: datetime) -> list[dict[str, Any]]:
        """Evaluate every live lot once at ``now``; return sell/skip records."""
        day = now.date()
        epoch = now.timestamp()
        sold_today = self._sold.setdefault(day, set())
        skipped_today = self._skipped.setdefault(day, set())

        symbols: list[str] = []
        for book in self.books:
            for symbol in book.lots:
                if symbol not in symbols:
                    symbols.append(symbol)
        snapshots = self.quotes.snapshot(symbols) if symbols else {}

        records: list[dict[str, Any]] = []
        for book in self.books:
            prices = self.engine_entry_prices.get(book.short, {})
            for symbol, lot in list(book.lots.items()):
                if lot.entry_date >= day:
                    # The engine also skips lots entered on the bar being run.
                    continue
                engine_price = prices.get(symbol)
                if engine_price is None:
                    if (book.short, symbol) in skipped_today:
                        continue
                    skipped_today.add((book.short, symbol))
                    logger.warning(
                        "%s %s 缺少引擎入场价, 跳过止损", book.short, symbol
                    )
                    records.append(
                        {
                            "symbol": symbol,
                            "short": book.short,
                            "key": lot.key,
                            "reason": "no_engine_entry_price",
                        }
                    )
                    continue

                # Own key: a morning exit for the same symbol and day uses
                # ``...-S``; a failed one must not block the stop.
                key = f"fw-{book.short}-{day:%Y%m%d}-{symbol}-S-stop"
                if key in sold_today:
                    continue

                snapshot = snapshots.get(symbol)
                last = _last_price(snapshot)
                if last is None:
                    continue

                stop = stop_line(engine_price)
                if last > stop:
                    continue

                updated = _epoch(snapshot.get("update_time") if snapshot else None)
                if updated is None or epoch - updated > STALE_QUOTE_S:
                    logger.warning(
                        "%s %s 快照过期 (stale quote, update_time=%r), 忽略止损", book.short, symbol, updated
                    )
                    continue

                shares = int(lot.shares)
                if shares <= 0:
                    continue

                if self.dry_run:
                    logger.warning(
                        "%s %s 试运行: 将卖出 %d 股 @市价 (止损 %.4f, 最新 %.4f)",
                        book.short,
                        symbol,
                        shares,
                        stop,
                        last,
                    )
                    sold_today.add(key)
                    records.append(
                        {
                            "symbol": symbol,
                            "stop": stop,
                            "last": last,
                            "key": key,
                            "shares": shares,
                            "dry_run": True,
                        }
                    )
                    continue

                try:
                    self.executor.submit(key, symbol, "S", shares)
                except Exception as exc:  # one lot must not stop the watch
                    logger.warning("%s %s 止损下单失败: %s", book.short, symbol, exc)
                    records.append(
                        {
                            "symbol": symbol,
                            "short": book.short,
                            "key": key,
                            "reason": "submit_failed",
                        }
                    )
                    continue

                sold_today.add(key)
                records.append(
                    {
                        "symbol": symbol,
                        "stop": stop,
                        "last": last,
                        "key": key,
                        "shares": shares,
                    }
                )
        return records

    # ── session loop ─────────────────────────────────────────
    def _sleep(self, seconds: float) -> None:
        sleeper = getattr(self.clock, "sleep", None)
        if callable(sleeper):
            sleeper(seconds)
        else:  # a bare market clock only knows the time
            time.sleep(seconds)

    def run_until(self, end_time: datetime) -> None:
        """Tick every ``poll_s`` until the clock reaches ``end_time``."""
        while True:
            now = self.clock.now()
            if now >= end_time:
                return
            self.tick(now)
            remaining = (end_time - now).total_seconds()
            self._sleep(max(0.0, min(self.poll_s, remaining)))


__all__ = ["STALE_QUOTE_S", "STOP_FRACTION", "StopWatch", "stop_line"]
