"""No-network fake of the moomoo trade context for forward tests.

Every trade call must carry ``trd_env=SIMULATE`` and the registered ``acc_id``;
the fake raises otherwise. It can inject the failures the forward runner has to
survive: a partial fill, a rejection, an exception raised after the broker
accepted the order, a dropped order (accepted but not queryable) and a
truncated ``remark``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from moomoo import TrdEnv

from tests.moomoo_fakes import FakeClock


def _symbol(code: str) -> str:
    return code[3:] if code.startswith("US.") else code


class FakeTradeContext:
    """Records calls and answers with plain lists of dicts (no pandas)."""

    def __init__(
        self,
        account_id: int = 1,
        *,
        accounts: list[dict[str, Any]] | None = None,
        cash: float = 1_000_000.0,
        positions: dict[str, int] | None = None,
        clock: FakeClock | None = None,
        orders: list[dict[str, Any]] | None = None,
        partial_fill_qty: int | None = None,
        accept_then_raise: BaseException | None = None,
        place_error: BaseException | None = None,
        reject: bool = False,
        drop_orders: bool = False,
        remark_limit: int | None = None,
        on_place: Callable[[], None] | None = None,
    ) -> None:
        self.account_id = account_id
        self.accounts = (
            list(accounts)
            if accounts is not None
            else [{"acc_id": account_id, "trd_env": TrdEnv.SIMULATE}]
        )
        self.cash = cash
        self.positions = dict(positions or {"AAPL": 100})
        self.clock = clock or FakeClock()
        self.orders = list(orders or [])
        self.partial_fill_qty = partial_fill_qty
        self.accept_then_raise = accept_then_raise
        self.place_error = place_error
        self.reject = reject
        self.drop_orders = drop_orders
        self.remark_limit = remark_limit
        self.on_place = on_place
        self.calls: list[dict[str, Any]] = []
        self._next_order = len(self.orders) + 1

    # ── call plumbing ────────────────────────────────────────
    def _record(
        self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any], *, trade: bool
    ) -> None:
        if trade:
            if "trd_env" not in kwargs:
                raise AssertionError(f"{name} called without trd_env")
            if kwargs["trd_env"] != TrdEnv.SIMULATE:
                raise AssertionError(f"{name} called with trd_env={kwargs['trd_env']!r}")
            if str(kwargs.get("acc_id")) != str(self.account_id):
                raise AssertionError(f"{name} called with acc_id={kwargs.get('acc_id')!r}")
        self.calls.append(
            {"name": name, "args": args, "kwargs": kwargs, "time": self.clock.monotonic()}
        )

    def calls_named(self, name: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["name"] == name]

    # ── trade-context surface ────────────────────────────────
    def get_acc_list(self) -> tuple[int, list[dict[str, Any]]]:
        self._record("get_acc_list", (), {}, trade=False)
        return 0, list(self.accounts)

    def accinfo_query(self, *args: Any, **kwargs: Any) -> tuple[int, list[dict[str, Any]]]:
        self._record("accinfo_query", args, kwargs, trade=True)
        return 0, [{"cash": self.cash, "currency": kwargs.get("currency")}]

    def position_list_query(
        self, *args: Any, **kwargs: Any
    ) -> tuple[int, list[dict[str, Any]]]:
        self._record("position_list_query", args, kwargs, trade=True)
        rows = [
            {"code": f"US.{symbol}", "qty": qty}
            for symbol, qty in self.positions.items()
            if qty
        ]
        return 0, rows

    def order_list_query(self, *args: Any, **kwargs: Any) -> tuple[int, list[dict[str, Any]]]:
        self._record("order_list_query", args, kwargs, trade=True)
        return 0, [dict(order) for order in self.orders]

    def place_order(self, *args: Any, **kwargs: Any) -> tuple[int, list[dict[str, Any]]]:
        self._record("place_order", args, kwargs, trade=True)
        if self.on_place is not None:
            self.on_place()
        if self.place_error is not None:
            raise self.place_error
        if self.reject:
            return 1, "REJECTED"
        order_id = f"ORD{self._next_order}"
        self._next_order += 1
        qty = int(kwargs.get("qty", args[1] if len(args) > 1 else 0))
        side = str(kwargs.get("trd_side", args[3] if len(args) > 3 else ""))
        code = str(kwargs.get("code", args[2] if len(args) > 2 else ""))
        remark = str(kwargs.get("remark") or "")
        if self.remark_limit is not None:
            remark = remark[: self.remark_limit]
        dealt = 0
        status = "SUBMITTED"
        avg_price = None
        if self.partial_fill_qty is not None:
            dealt = min(int(self.partial_fill_qty), qty)
            status = "FILLED_PART" if dealt < qty else "FILLED_ALL"
            avg_price = 100.0
        order = {
            "order_id": order_id,
            "code": code,
            "remark": remark,
            "order_status": status,
            "qty": qty,
            "dealt_qty": dealt,
            "dealt_avg_price": avg_price,
            "trd_side": side,
        }
        if not self.drop_orders:
            self.orders.append(order)
        if self.accept_then_raise is not None:
            raise self.accept_then_raise
        return 0, [{"order_id": order_id}]

    def modify_order(self, *args: Any, **kwargs: Any) -> tuple[int, list[dict[str, Any]]]:
        self._record("modify_order", args, kwargs, trade=True)
        order_id = str(kwargs.get("order_id", args[1] if len(args) > 1 else ""))
        for order in self.orders:
            if str(order["order_id"]) == order_id:
                order["order_status"] = "CANCELLED_ALL"
                return 0, [dict(order)]
        return 1, f"order {order_id} not found"

    def close(self) -> None:
        self._record("close", (), {}, trade=False)


__all__ = ["FakeTradeContext", "_symbol"]