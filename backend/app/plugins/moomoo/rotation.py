"""Subscription rotation for moomoo daily bars.

moomoo only serves klines for codes held by a live K_DAY subscription, and the
free subscription quota is shared with every other app on the machine. This
module therefore reads the quota before each batch, subscribes, downloads raw
and QFQ bars, keeps the subscription for at least 61 s (OpenD requirement),
then unsubscribes and yields the batch.

The generator never raises for a single bad symbol: failed codes are collected
in ``BatchResult.failed`` so the sync can record them and continue with the
next batch.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import polars as pl

from app.plugins.moomoo.provider import _unpack_ret

KLINES_PER_SYMBOL = 1000
RATE_LIMIT_SLEEP_S = 5.0


@dataclass
class BatchResult:
    """One subscription batch: downloaded bars per code plus failed codes."""

    bars: dict[str, tuple[pl.DataFrame, pl.DataFrame]] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)


def _free_quota(ctx: object) -> int:
    """Free subscription slots, re-read before every batch."""
    ret, data = _unpack_ret(ctx.query_subscription())  # type: ignore[attr-defined]
    if ret != 0:
        raise RuntimeError(f"query_subscription 失败: ret={ret!r} data={data!r}")
    if not isinstance(data, dict):
        raise RuntimeError(f"query_subscription 返回非字典: {data!r}")
    try:
        return max(0, int(data.get("remain") or 0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"query_subscription remain 非法: {data!r}") from exc


def _fetch_kline(
    ctx: object,
    code: str,
    autype: object,
    ktype: object,
    *,
    sleep: Callable[[float], None],
) -> pl.DataFrame:
    """One ``get_cur_kline``; backs off once when OpenD reports a rate limit."""
    ret, data = _unpack_ret(
        ctx.get_cur_kline(code, KLINES_PER_SYMBOL, ktype, autype)  # type: ignore[attr-defined]
    )
    if ret != 0 and "frequen" in str(data).lower():
        sleep(RATE_LIMIT_SLEEP_S)
        ret, data = _unpack_ret(
            ctx.get_cur_kline(code, KLINES_PER_SYMBOL, ktype, autype)  # type: ignore[attr-defined]
        )
    if ret != 0:
        raise RuntimeError(str(data))
    return data


def rotate_daily(
    ctx: object,
    codes: list[str],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    hold_s: float = 61.0,
) -> Iterator[BatchResult]:
    """Yield one ``BatchResult`` per subscription batch of at most the free quota."""
    import moomoo

    pending = list(codes)
    while pending:
        quota = _free_quota(ctx)
        if quota <= 0:
            raise RuntimeError("moomoo subscription quota exhausted")
        batch, pending = pending[:quota], pending[quota:]
        result = BatchResult()

        try:
            ret, _data = _unpack_ret(
                ctx.subscribe(  # type: ignore[attr-defined]
                    batch, [moomoo.SubType.K_DAY], subscribe_push=False
                )
            )
        except Exception:  # OpenD may be down mid-run
            result.failed.extend(batch)
            yield result
            continue
        if ret != 0:
            result.failed.extend(batch)
            yield result
            continue

        started = clock()
        for code in batch:
            try:
                raw = _fetch_kline(ctx, code, moomoo.AuType.NONE, moomoo.KLType.K_DAY, sleep=sleep)
                qfq = _fetch_kline(ctx, code, moomoo.AuType.QFQ, moomoo.KLType.K_DAY, sleep=sleep)
            except Exception:  # one bad code must not kill the batch
                result.failed.append(code)
                continue
            result.bars[code] = (raw, qfq)

        delay = started + hold_s - clock()
        if delay > 0:
            sleep(delay)
        ctx.unsubscribe(batch, [moomoo.SubType.K_DAY])  # type: ignore[attr-defined]
        yield result
