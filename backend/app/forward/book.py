"""Live book and reconciliation per strategy.

Maintains each strategy's live lots and cash balance, reconstructed from the
append-only order journal. Reconciles live lots across strategies against
broker positions at 16:15, handling corporate actions (stock splits) and
freezing symbols with unexplained mismatches.
"""

from __future__ import annotations

import json
import math
import os
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

from app.forward.orders import _state as _orders_state


def _parse_date(val: Any) -> date:
    if isinstance(val, date):
        return val
    s = str(val).strip().replace("-", "")
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return date.fromisoformat(str(val).strip())


class Lot(NamedTuple):
    symbol: str
    shares: int
    entry_date: date
    entry_price: float
    key: str


# One status vocabulary for the book and the executor.
_state = _orders_state


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


def _extract_short(key: str) -> str | None:
    if not key.startswith("fw-"):
        return None
    parts = key.split("-")
    if len(parts) >= 2:
        return parts[1]
    return None


def _extract_symbol(key: str) -> str:
    if not key.startswith("fw-"):
        return ""
    parts = key.split("-")
    if len(parts) >= 4:
        return parts[3]
    return ""


def _extract_side(key: str) -> str:
    if not key.startswith("fw-"):
        return ""
    parts = key.split("-")
    if len(parts) >= 5:
        return _side(parts[4])
    return ""


def _extract_date(event: dict[str, Any], key: str) -> date:
    raw = event.get("entry_date") or event.get("date")
    if raw:
        return _parse_date(raw)
    if key.startswith("fw-"):
        parts = key.split("-")
        if len(parts) >= 3 and len(parts[2]) == 8 and parts[2].isdigit():
            d = parts[2]
            return date(int(d[:4]), int(d[4:6]), int(d[6:8]))
    return date.today()


class LiveBook:
    """Per-strategy live portfolio book."""

    def __init__(
        self,
        short: str,
        capital: float,
        lots: dict[str, Lot] | None = None,
        cash: float | None = None,
    ) -> None:
        self.short = str(short)
        self.capital = float(capital)
        self.lots: dict[str, Lot] = dict(lots) if lots is not None else {}
        self.cash: float = float(capital if cash is None else cash)
        self.unknown_orders: list[dict[str, Any]] = []
        self._dealt_by_key: dict[str, int] = {}

    def equity(self, prices: dict[str, float] | None = None) -> float:
        """Current equity valuing open lots at given or entry prices."""
        px = prices or {}
        val = self.cash + sum(
            lot.shares * px.get(lot.symbol, lot.entry_price)
            for lot in self.lots.values()
        )
        return float(val)

    def apply_fill(self, event: dict[str, Any]) -> None:
        """Apply an order event or fill to live lots and cash."""
        key = str(event.get("key") or "")
        if key.startswith("fw-flatten-"):
            return

        short = _extract_short(key)
        if short is not None and short not in ("flatten", "") and short != self.short:
            return

        state = _state(event)
        if state == "unknown":
            # Unresolved, but any quantity already reported as dealt is real.
            self.unknown_orders.append(dict(event))

        if state not in ("filled", "cancelled", "partial", "unknown"):
            return

        dealt_qty = _int(event.get("dealt_qty"))
        if dealt_qty <= 0:
            return

        dealt_avg_price = _float(event.get("dealt_avg_price"))
        if dealt_avg_price is None or dealt_avg_price <= 0:
            dealt_avg_price = _float(event.get("price"))
        if dealt_avg_price is None or dealt_avg_price <= 0:
            return

        symbol = str(event.get("symbol") or event.get("code") or "")
        if not symbol:
            symbol = _extract_symbol(key)
        if not symbol:
            return

        side = _side(event.get("side") or event.get("trd_side"))
        if not side and key:
            side = _extract_side(key)
        if not side:
            return

        prev_dealt = self._dealt_by_key.get(key, 0) if key else 0
        delta_qty = dealt_qty - prev_dealt
        if delta_qty <= 0:
            return
        if key:
            self._dealt_by_key[key] = dealt_qty

        if side == "B":
            self.cash -= delta_qty * dealt_avg_price
            entry_date = _extract_date(event, key)
            if symbol in self.lots:
                curr = self.lots[symbol]
                self.lots[symbol] = Lot(
                    symbol=symbol,
                    shares=curr.shares + delta_qty,
                    entry_date=curr.entry_date,
                    entry_price=curr.entry_price,
                    key=curr.key,
                )
            else:
                self.lots[symbol] = Lot(
                    symbol=symbol,
                    shares=delta_qty,
                    entry_date=entry_date,
                    entry_price=dealt_avg_price,
                    key=key,
                )
        elif side == "S":
            if symbol in self.lots:
                curr = self.lots[symbol]
                sold_shares = min(curr.shares, delta_qty)
                self.cash += sold_shares * dealt_avg_price
                rem = curr.shares - sold_shares
                if rem > 0:
                    self.lots[symbol] = Lot(
                        symbol=symbol,
                        shares=rem,
                        entry_date=curr.entry_date,
                        entry_price=curr.entry_price,
                        key=curr.key,
                    )
                else:
                    del self.lots[symbol]

    def scale(
        self, symbol: str, ratio: float, reason: str = ""
    ) -> dict[str, Any] | None:
        """Scale lot shares by ratio (e.g. stock split), flooring to whole shares."""
        if symbol not in self.lots:
            return None
        ratio = float(ratio)
        if ratio <= 0:
            raise ValueError(f"Scaling ratio must be positive: {ratio}")
        lot = self.lots[symbol]
        exact_shares = lot.shares * ratio
        new_shares = math.floor(exact_shares)
        remainder = exact_shares - new_shares
        new_price = lot.entry_price / ratio
        if new_shares > 0:
            self.lots[symbol] = Lot(
                symbol=symbol,
                shares=int(new_shares),
                entry_date=lot.entry_date,
                entry_price=float(new_price),
                key=lot.key,
            )
        else:
            del self.lots[symbol]
        return {
            "symbol": symbol,
            "old_shares": lot.shares,
            "new_shares": int(new_shares),
            "ratio": ratio,
            "reason": reason,
            "remainder": remainder,
        }

    def close_corporate(self, symbol: str, cash_amount: float) -> None:
        """Close a lot due to cash merger or delisting."""
        self.cash += float(cash_amount)
        if symbol in self.lots:
            del self.lots[symbol]

    @classmethod
    def from_journal(
        cls,
        short: str,
        capital: float,
        events: list[dict[str, Any]],
    ) -> LiveBook:
        """Reconstruct a LiveBook from order journal events."""
        book = cls(short=short, capital=capital)
        events_by_key: dict[str, list[dict[str, Any]]] = {}
        no_key_events: list[dict[str, Any]] = []

        for event in events:
            key = str(event.get("key") or "")
            if not key:
                no_key_events.append(event)
                continue
            events_by_key.setdefault(key, []).append(event)

        for event in no_key_events:
            book.apply_fill(event)

        for key, key_events in events_by_key.items():
            if key.startswith("fw-flatten-"):
                continue
            k_short = _extract_short(key)
            if (
                k_short is not None
                and k_short not in ("flatten", "")
                and k_short != short
            ):
                continue

            merged: dict[str, Any] = {}
            for e in key_events:
                for k, v in e.items():
                    if v is not None:
                        merged[k] = v

            latest_event = key_events[-1]
            latest_state = _state(latest_event)
            merged["state"] = latest_state
            book.apply_fill(merged)

        return book


def reconcile(
    books: list[LiveBook],
    broker_positions: dict[str, int],
    broker_cash: float,
    adj_events: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Reconcile live books across strategies against broker positions and cash."""
    adj_events = adj_events or {}
    scaled: list[dict[str, Any]] = []
    frozen_symbols: list[str] = []
    mismatches: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []

    for book in books:
        for unk in getattr(book, "unknown_orders", []):
            attention.append(
                {
                    "issue": "unknown_order",
                    "book": book.short,
                    "key": unk.get("key"),
                    "symbol": unk.get("symbol"),
                    "order": unk,
                }
            )

    if broker_cash < 0:
        attention.append(
            {
                "issue": "negative_broker_cash",
                "broker_cash": broker_cash,
            }
        )

    all_symbols = sorted(
        set(broker_positions) | {sym for b in books for sym in b.lots}
    )

    for symbol in all_symbols:
        broker_qty = int(broker_positions.get(symbol, 0))
        book_qty = sum(b.lots[symbol].shares for b in books if symbol in b.lots)

        if broker_qty == book_qty:
            continue

        ratio = adj_events.get(symbol)
        if ratio is not None and ratio > 0:
            for b in books:
                if symbol in b.lots:
                    info = b.scale(symbol, ratio, reason="split")
                    if info:
                        scaled.append(info)
            new_book_qty = sum(b.lots[symbol].shares for b in books if symbol in b.lots)
            if broker_qty == new_book_qty:
                continue
            mismatches.append(
                {"symbol": symbol, "broker": broker_qty, "books": new_book_qty}
            )
            frozen_symbols.append(symbol)
        else:
            mismatches.append(
                {"symbol": symbol, "broker": broker_qty, "books": book_qty}
            )
            frozen_symbols.append(symbol)

    ok = (len(mismatches) == 0) and (len(frozen_symbols) == 0) and (len(attention) == 0)
    return {
        "ok": ok,
        "mismatches": mismatches,
        "scaled": scaled,
        "frozen_symbols": frozen_symbols,
        "attention": attention,
    }


def save_book(
    path: Path | str, book: LiveBook, *, journal_events: int | None = None
) -> Path:
    """Save LiveBook state to a JSON file atomically.

    ``journal_events`` is how many order-journal events the book was rebuilt
    from, so a later check can rebuild from exactly those events: the journal
    keeps growing (stop fills) after the book is saved.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "short": book.short,
        "capital": book.capital,
        "cash": book.cash,
        "journal_events": journal_events,
        "lots": {
            sym: {
                "symbol": lot.symbol,
                "shares": lot.shares,
                "entry_date": lot.entry_date.isoformat()
                if isinstance(lot.entry_date, date)
                else str(lot.entry_date),
                "entry_price": lot.entry_price,
                "key": lot.key,
            }
            for sym, lot in sorted(book.lots.items())
        },
    }
    tmp_path = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    os.replace(tmp_path, p)
    return p


def load_book(path: Path | str) -> LiveBook:
    """Load LiveBook state from a JSON file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Book file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    lots: dict[str, Lot] = {}
    for sym, item in data.get("lots", {}).items():
        lots[sym] = Lot(
            symbol=item["symbol"],
            shares=int(item["shares"]),
            entry_date=_parse_date(item["entry_date"]),
            entry_price=float(item["entry_price"]),
            key=item["key"],
        )
    return LiveBook(
        short=data["short"],
        capital=float(data["capital"]),
        lots=lots,
        cash=float(data["cash"]),
    )


def saved_journal_events(path: Path | str) -> int | None:
    """The journal length recorded by ``save_book`` (``None`` if absent)."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8")).get("journal_events")
    except (OSError, ValueError, AttributeError):
        return None
    return int(value) if isinstance(value, int) else None


def check_saved_matches_journal(path: Path | str, book: LiveBook) -> bool:
    """Check that saved book on disk matches the book rebuilt from journal."""
    p = Path(path)
    if not p.exists():
        return False
    try:
        saved = load_book(p)
    except Exception:
        return False

    if saved.short != book.short:
        return False
    if abs(saved.capital - book.capital) > 1e-4:
        return False
    if abs(saved.cash - book.cash) > 1e-4:
        return False
    if set(saved.lots.keys()) != set(book.lots.keys()):
        return False
    for sym, b_lot in book.lots.items():
        s_lot = saved.lots[sym]
        if s_lot.symbol != b_lot.symbol:
            return False
        if s_lot.shares != b_lot.shares:
            return False
        if abs(s_lot.entry_price - b_lot.entry_price) > 1e-4:
            return False
        if str(s_lot.entry_date) != str(b_lot.entry_date):
            return False
        if s_lot.key != b_lot.key:
            return False
    return True


__all__ = [
    "LiveBook",
    "Lot",
    "check_saved_matches_journal",
    "load_book",
    "reconcile",
    "save_book",
    "saved_journal_events",
]
