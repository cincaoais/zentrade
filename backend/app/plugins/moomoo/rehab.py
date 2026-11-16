"""``get_rehab`` rows → adjustment-event rows.

OpenD's ``get_rehab`` returns the full lifetime of per-event rows
(``ex_div_date``, ``split_ratio``, ``per_cash_div``, ``forward_adj_factorA/B``).
This module maps them to the event contract shared with ``kline_sync``:
one row per ex-date with the factors multiplied together.

A symbol fails closed (``ValueError``) when a row carries an additive factor
(``forward_adj_factorB != 0``) or looks like a split without ``split_ratio``:
either case means the vendor's factor semantics are not the ones this mapping
was verified against.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any


def _number(row: dict[str, Any], key: str) -> float | None:
    """Cell → float; missing/blank/NaN → ``None``; anything else non-numeric raises."""
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"nan", "none", "nat", "n/a", "-"}:
            return None
        try:
            number = float(text)
        except ValueError as exc:
            raise ValueError(f"{key} 不是数字: {value!r}") from exc
    else:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 不是数字: {value!r}") from exc
    return None if math.isnan(number) else number


def _trade_date(value: Any) -> dt.date:
    """``ex_div_date`` (date, datetime or ISO-ish string) → date."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip()
    if not text or text.lower() in {"nat", "nan", "none", "n/a", "-"}:
        raise ValueError(f"ex_div_date 缺失: {value!r}")
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"ex_div_date 无法解析: {value!r}") from exc


def rehab_events(symbol: str, rows: list[dict]) -> list[dict]:
    """Group rehab rows by ex-date and multiply the per-event factors.

    ``ex_factor = Π 1/forward_adj_factorA``;
    ``vol_factor = Π 1/split_ratio`` over rows that actually carry a split ratio.
    """
    order: list[dt.date] = []
    ex_factors: dict[dt.date, float] = {}
    vol_factors: dict[dt.date, float] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        day = _trade_date(row.get("ex_div_date"))

        factor_b = _number(row, "forward_adj_factorB")
        if factor_b is not None and factor_b != 0:
            raise ValueError(f"{symbol} {day}: forward_adj_factorB={factor_b!r} 不受支持")

        factor_a = _number(row, "forward_adj_factorA")
        if factor_a is None or not math.isfinite(factor_a) or factor_a <= 0:
            raise ValueError(f"{symbol} {day}: forward_adj_factorA 非法: {row.get('forward_adj_factorA')!r}")

        split_ratio = _number(row, "split_ratio")
        split_base = _number(row, "split_base")
        has_split_base = split_base is not None and split_base != 0
        if has_split_base and split_ratio is None:
            raise ValueError(f"{symbol} {day}: 拆股行缺少 split_ratio")
        if split_ratio is not None and (not math.isfinite(split_ratio) or split_ratio <= 0):
            raise ValueError(f"{symbol} {day}: split_ratio 非法: {row.get('split_ratio')!r}")

        if day not in ex_factors:
            order.append(day)
            ex_factors[day] = 1.0
            vol_factors[day] = 1.0
        ex_factors[day] *= 1.0 / factor_a
        if split_ratio is not None:
            vol_factors[day] *= 1.0 / split_ratio

    return [
        {
            "symbol": symbol,
            "trade_date": day,
            "ex_factor": ex_factors[day],
            "vol_factor": vol_factors[day],
        }
        for day in sorted(order)
    ]