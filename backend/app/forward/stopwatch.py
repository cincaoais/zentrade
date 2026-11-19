"""Intraday -15% stop watch (spec S1).

For every live lot, when the last trade at or below ``entry_price * 0.85`` the
watch sends one market sell for that lot. The line uses the **engine's** entry
price, not the live fill price, so the live stop and the research stop sit on
the same line (spec 6.5). Engine entry prices are supplied by the caller from
the saved decision records; a lot without one is never sold on a guessed line:
it is skipped and reported with ``reason="no_engine_entry_price"``.

Quotes older than ``STALE_QUOTE_S`` are ignored and logged. Each lot is sold at
most once per trading day (the order key is deterministic and the executor is
idempotent as well). Sent stop orders are read back every tick and once more at
the end of the session, so their fills reach the order journal and the next
day's book. The kill flag (``forward/STOP``) blocks new stop orders (spec 6.8:
checked before every order). One failed tick is logged in ``errors`` and the
watch carries on.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
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
        stop_flag: Path | None = None,
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
        self.stop_flag = Path(stop_flag) if stop_flag is not None else None
        self._flagged: set[date] = set()
        # Stop keys sent and not yet final: key -> (book short, symbol).
        self._pending: dict[str, tuple[str, str]] = {}
        self.errors: list[str] = []

    # ── sent stop orders ─────────────────────────────────────
    def settle(self) -> None:
        """Read sent stop orders back; drop the lot of a filled one from its book."""
        if not self._pending:
            return
        states = self.executor.refresh(keys=list(self._pending))
        for key, state in states.items():
            if state in {"filled", "cancelled", "failed", "skipped"}:
                short, symbol = self._pending.pop(key)
                if state == "filled":
                    for book in self.books:
                        if book.short == short:
                            book.lots.pop(symbol, None)

    # ── per-tick decision ────────────────────────────────────
    def tick(self, now: datetime) -> list[dict[str, Any]]:
        """Evaluate every live lot once at ``now``; return sell/skip records."""
        day = now.date()
        epoch = now.timestamp()
        sold_today = self._sold.setdefault(day, set())
        skipped_today = self._skipped.setdefault(day, set())
        if not self.dry_run:
            self.settle()

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

                if self.stop_flag is not None and self.stop_flag.exists():
                    if day not in self._flagged:
                        self._flagged.add(day)
                        logger.warning("%s %s 触及止损但存在停止标志, 不下单", book.short, symbol)
                        records.append(
                            {"symbol": symbol, "short": book.short, "key": key, "reason": "kill_flag"}
                        )
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
                    outcome = self.executor.submit(key, symbol, "S", shares)
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
                if outcome == "skipped":
                    logger.warning("%s %s 触及止损但券商没有可卖股数", book.short, symbol)
                    records.append(
                        {"symbol": symbol, "short": book.short, "key": key, "reason": "broker_holds_none"}
                    )
                    continue
                self._pending[key] = (book.short, symbol)
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
        """Tick every ``poll_s`` until the clock reaches ``end_time``.

        A failed tick (quote or broker error) is logged and kept in ``errors``;
        the watch goes on. Sent stop orders are read back once more at the end.
        """
        while True:
            now = self.clock.now()
            if now >= end_time:
                break
            try:
                self.tick(now)
            except Exception as exc:  # one bad tick must not end the day's stops
                logger.exception("止损轮询失败")
                self.errors.append(f"{now.isoformat()} {type(exc).__name__}: {exc}")
            remaining = end_time.timestamp() - now.timestamp()
            self._sleep(max(0.0, min(self.poll_s, remaining)))
        if not self.dry_run:
            try:
                self.settle()
            except Exception as exc:
                logger.exception("收盘读取止损单失败")
                self.errors.append(f"settle {type(exc).__name__}: {exc}")


__all__ = ["STALE_QUOTE_S", "STOP_FRACTION", "StopWatch", "stop_line"]
