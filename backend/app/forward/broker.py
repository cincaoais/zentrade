"""The only module that may touch the moomoo trade SDK.

Every trade call hard-codes the SIMULATE environment and the registered account
id: the SDK's defaults are REAL, so nothing here may rely on a default. Symbols
cross the boundary through the moomoo provider's ``to_moomoo`` /
``from_moomoo`` helpers.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Mapping
from typing import Any

from moomoo import (
    ModifyOrderOp,
    OpenQuoteContext,
    OpenSecTradeContext,
    OrderType,
    TrdEnv,
    TrdMarket,
    TrdSide,
)

from app.market_clock import us_clock
from app.plugins.moomoo.provider import from_moomoo, opend_address, to_moomoo

# moomoo snapshot limit: 400 codes per call.
SNAPSHOT_CHUNK = 400


class BrokerError(RuntimeError):
    """A moomoo call failed or returned an unusable payload."""


class NotSimulate(BrokerError):  # noqa: N818
    """The registered account id is not a SIMULATE account."""


# ── SDK payload helpers ───────────────────────────────────────
def _rows(data: Any) -> list[dict[str, Any]]:
    """SDK frames/lists/dicts → a list of plain row dicts."""
    if data is None:
        return []
    if isinstance(data, Mapping):
        return [dict(data)]
    to_dict = getattr(data, "to_dict", None)
    if callable(to_dict):
        try:
            records = to_dict("records")
        except (TypeError, ValueError):
            records = to_dict()
        if isinstance(records, Mapping):
            return [dict(records)]
        if isinstance(records, list):
            return [dict(row) for row in records if isinstance(row, Mapping)]
    if isinstance(data, (list, tuple)):
        return [dict(row) for row in data if isinstance(row, Mapping)]
    return []


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value: Any) -> int:
    parsed = _number(value)
    return int(parsed) if parsed is not None else 0


def _epoch_seconds(value: Any) -> float | None:
    """SDK ``update_time`` → epoch seconds; a naive wall clock is US/Eastern."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text or text.lower() in {"n/a", "none", "nat", "nan", "-"}:
            return None
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=us_clock.tz)
    return parsed.timestamp()


def _unpack(response: Any, what: str) -> list[dict[str, Any]]:
    try:
        ret, data = response
    except (TypeError, ValueError) as exc:
        raise BrokerError(f"{what}: 无法解析 SDK 返回 {response!r}") from exc
    if ret != 0:
        raise BrokerError(f"{what} 失败: ret={ret!r} data={data!r}")
    return _rows(data)


def _default_trade_context() -> Any:
    host, port = opend_address()
    return OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=host, port=port)


def _default_quote_context() -> Any:
    host, port = opend_address()
    return OpenQuoteContext(host=host, port=port)


class SimBroker:
    """Thin SIMULATE-only wrapper over ``OpenSecTradeContext``."""

    def __init__(
        self,
        account_id: int,
        ctx_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.account_id = int(account_id)
        self._ctx_factory = ctx_factory or _default_trade_context
        self._ctx: Any | None = None

    def _context(self) -> Any:
        if self._ctx is None:
            self._ctx = self._ctx_factory()
        return self._ctx

    def _call(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        # Never inherit the SDK default: REAL is the default for every call.
        kwargs["trd_env"] = TrdEnv.SIMULATE
        kwargs["acc_id"] = self.account_id
        return _unpack(method(*args, **kwargs), getattr(method, "__name__", "moomoo"))

    def close(self) -> None:
        if self._ctx is not None:
            close = getattr(self._ctx, "close", None)
            if callable(close):
                close()
            self._ctx = None

    # ── account reads ────────────────────────────────────────
    def check_account(self) -> dict[str, Any]:
        """Return the registered account, refusing anything but SIMULATE."""
        ret, data = self._context().get_acc_list()
        if ret != 0:
            raise BrokerError(f"get_acc_list 失败: ret={ret!r} data={data!r}")
        record = next(
            (row for row in _rows(data) if str(row.get("acc_id")) == str(self.account_id)),
            None,
        )
        if record is None:
            raise NotSimulate(f"账户 {self.account_id} 不在 get_acc_list 中")
        if str(record.get("trd_env", "")).strip().upper() != TrdEnv.SIMULATE:
            raise NotSimulate(
                f"账户 {self.account_id} 不是 SIMULATE 账户: trd_env={record.get('trd_env')!r}"
            )
        account = dict(record)
        account["acc_id"] = self.account_id
        account["trd_env"] = TrdEnv.SIMULATE
        return account

    def cash(self) -> float:
        rows = self._call(self._context().accinfo_query, currency="USD")
        if not rows:
            raise BrokerError("accinfo_query 没有返回账户行")
        value = _number(rows[0].get("cash"))
        if value is None:
            raise BrokerError(f"accinfo_query 没有 cash: {rows[0]!r}")
        return value

    def positions(self) -> dict[str, int]:
        rows = self._call(self._context().position_list_query, currency="USD")
        positions: dict[str, int] = {}
        for row in rows:
            code = str(row.get("code") or "")
            if not code:
                continue
            try:
                symbol = from_moomoo(code)
            except ValueError:
                symbol = code
            qty = _integer(row.get("qty"))
            if qty:
                positions[symbol] = qty
        return positions

    # ── orders ───────────────────────────────────────────────
    def place_market(self, symbol: str, side: str, qty: int, remark: str) -> str:
        raw_side = str(side or "").strip().upper()
        if raw_side in {"B", "BUY", "BUY_BACK"}:
            trd_side = TrdSide.BUY
        elif raw_side in {"S", "SELL", "SELL_SHORT"}:
            trd_side = TrdSide.SELL
        else:
            raise ValueError(f"未知交易方向: {side!r}")
        shares = int(qty)
        if shares <= 0:
            raise ValueError(f"数量必须为正: {qty!r}")
        rows = self._call(
            self._context().place_order,
            price=0,
            qty=shares,
            code=to_moomoo(symbol),
            trd_side=trd_side,
            order_type=OrderType.MARKET,
            remark=remark,
            time_in_force="DAY",
            fill_outside_rth=False,
        )
        if not rows:
            raise BrokerError("place_order 没有返回委托")
        order_id = rows[0].get("order_id")
        if order_id is None:
            raise BrokerError(f"place_order 没有 order_id: {rows[0]!r}")
        return str(order_id)

    def orders_today(self) -> list[dict[str, Any]]:
        rows = self._call(self._context().order_list_query)
        orders: list[dict[str, Any]] = []
        for row in rows:
            code = str(row.get("code") or "")
            try:
                symbol = from_moomoo(code)
            except ValueError:
                symbol = code
            orders.append(
                {
                    "order_id": str(row.get("order_id", "")),
                    "code": symbol,
                    "remark": str(row.get("remark") or ""),
                    "status": str(
                        row.get("order_status") or row.get("status") or ""
                    ).upper(),
                    "qty": _integer(row.get("qty")),
                    "dealt_qty": _integer(row.get("dealt_qty")),
                    "dealt_avg_price": _number(row.get("dealt_avg_price")),
                    "trd_side": str(row.get("trd_side") or "").upper(),
                }
            )
        return orders

    def cancel(self, order_id: str) -> None:
        self._call(
            self._context().modify_order,
            modify_order_op=ModifyOrderOp.CANCEL,
            order_id=str(order_id),
            qty=0,
            price=0,
        )


class Quotes:
    """Snapshot reader; the only quote calls the forward runner makes."""

    def __init__(self, ctx_factory: Callable[[], Any] | None = None) -> None:
        self._ctx_factory = ctx_factory or _default_quote_context
        self._ctx: Any | None = None

    def _context(self) -> Any:
        if self._ctx is None:
            self._ctx = self._ctx_factory()
        return self._ctx

    def close(self) -> None:
        if self._ctx is not None:
            close = getattr(self._ctx, "close", None)
            if callable(close):
                close()
            self._ctx = None

    def snapshot(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """Return ``{symbol: {open_price, last_price, update_time}}``.

        ``update_time`` is epoch seconds so callers can compare it with a
        monotonic/epoch clock; a missing or unparsable time is ``None``.
        """
        codes = [to_moomoo(symbol) for symbol in symbols]
        if not codes:
            return {}
        rows: list[dict[str, Any]] = []
        for start in range(0, len(codes), SNAPSHOT_CHUNK):
            chunk = codes[start : start + SNAPSHOT_CHUNK]
            rows.extend(_unpack(self._context().get_market_snapshot(chunk), "get_market_snapshot"))
        snapshots: dict[str, dict[str, Any]] = {}
        for row in rows:
            code = str(row.get("code") or "")
            try:
                symbol = from_moomoo(code)
            except ValueError:
                continue
            snapshots[symbol] = {
                "open_price": _number(row.get("open_price")),
                "last_price": _number(row.get("last_price")),
                "update_time": _epoch_seconds(row.get("update_time")),
            }
        return snapshots


__all__ = ["BrokerError", "NotSimulate", "Quotes", "SimBroker"]