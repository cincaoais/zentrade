"""Live OpenD verification (opt-in).

Skipped unless ``ZENTRADE_MOOMOO_LIVE=1``. This is the only test module that
talks to a real OpenD, and it uses quote APIs only: ``get_cur_kline`` through
the subscription rotation, ``get_rehab`` and ``get_stock_basicinfo``. It never
calls a trading API.

The rotation keeps each subscription batch for at least 61 s, so a live run
takes over a minute even for four symbols.
"""

from __future__ import annotations

import math
import os

import polars as pl
import pytest

from app.plugins.moomoo.provider import (
    MOOMOO_MIN_REL,
    MOOMOO_QFQ_TICK,
    _basicinfo_rows,
    _canonical_daily,
    _event_bars,
    _unpack_ret,
    from_moomoo,
    open_ctx,
    to_moomoo,
)
from app.plugins.moomoo.rehab import rehab_events
from app.plugins.moomoo.rotation import rotate_daily
from app.services.adjustment_events import events_for_symbols

LIVE = os.environ.get("ZENTRADE_MOOMOO_LIVE") == "1"
pytestmark = pytest.mark.skipif(
    not LIVE, reason="live OpenD test: set ZENTRADE_MOOMOO_LIVE=1"
)

SYMBOLS = ["AAPL", "NVDA", "BRK.B", "KO"]
QFQ_REL_TOL = 1e-6
REHAB_REL_TOL = 1e-4
ADJ_SCHEMA = {
    "symbol": pl.Utf8,
    "trade_date": pl.Date,
    "ex_factor": pl.Float64,
    "vol_factor": pl.Float64,
}


@pytest.fixture(scope="module")
def downloaded() -> dict[str, tuple[object, object]]:
    """One rotation pass: raw and QFQ frames per bare symbol."""
    with open_ctx() as ctx:
        batches = list(rotate_daily(ctx, [to_moomoo(symbol) for symbol in SYMBOLS]))

    failed = [code for batch in batches for code in batch.failed]
    assert not failed, f"OpenD failed to return bars for {failed}"

    frames: dict[str, tuple[object, object]] = {}
    for batch in batches:
        for code, (raw, qfq) in batch.bars.items():
            frames[from_moomoo(code)] = (raw, qfq)
    assert set(frames) == set(SYMBOLS), f"missing bars: {set(SYMBOLS) - set(frames)}"
    return frames


@pytest.fixture(scope="module")
def derived_events(
    downloaded: dict[str, tuple[object, object]],
) -> pl.DataFrame:
    """Event rows derived from the raw/QFQ pair, exactly like the daily sync."""
    events: list[dict] = []
    for symbol, (raw, qfq) in downloaded.items():
        events.extend(
            events_for_symbols(
                ((symbol, bar) for bar in _event_bars(raw)),
                ((symbol, bar) for bar in _event_bars(qfq)),
                skip_invalid_symbols=False,
                price_tick=MOOMOO_QFQ_TICK,
                min_rel=MOOMOO_MIN_REL,
            )
        )
    if not events:
        return pl.DataFrame(schema=ADJ_SCHEMA)
    return pl.DataFrame(events, schema=ADJ_SCHEMA)


def _forward_adjust(raw: pl.DataFrame, events: pl.DataFrame) -> pl.DataFrame:
    """``adjusted = raw * cum_at_D / total_cum`` (see indicators/pipeline.py).

    ``cum_at_D`` is the cumulative product of ``ex_factor`` over events up to
    and including the bar's date (as-of backward join).
    """
    if events.is_empty():
        return raw.with_columns(pl.col("close").alias("adjusted"))

    factors = (
        events.select("symbol", "trade_date", "ex_factor")
        .sort(["symbol", "trade_date"])
        .with_columns(pl.col("ex_factor").cum_prod().over("symbol").alias("cum_factor"))
    )
    totals = factors.group_by("symbol").agg(
        pl.col("cum_factor").last().alias("total_factor")
    )
    joined = raw.sort(["symbol", "date"]).join_asof(
        factors.select("symbol", "trade_date", "cum_factor"),
        left_on="date",
        right_on="trade_date",
        by="symbol",
        strategy="backward",
    )
    return joined.join(totals, on="symbol", how="left").with_columns(
        (
            pl.col("close")
            * pl.col("cum_factor").fill_null(1.0)
            / pl.col("total_factor").fill_null(1.0)
        ).alias("adjusted")
    )


def test_live_events_reproduce_qfq(
    downloaded: dict[str, tuple[object, object]], derived_events: pl.DataFrame
) -> None:
    raw_all = pl.concat(
        [_canonical_daily(symbol, raw).filter(pl.col("date").is_not_null())
         for symbol, (raw, _qfq) in downloaded.items()]
    )
    qfq_all = pl.concat(
        [_canonical_daily(symbol, qfq).filter(pl.col("date").is_not_null())
         for symbol, (_raw, qfq) in downloaded.items()]
    )
    adjusted = _forward_adjust(raw_all, derived_events)

    failures: list[str] = []
    summary: list[str] = []
    for symbol in SYMBOLS:
        raw = adjusted.filter(pl.col("symbol") == symbol)
        qfq = qfq_all.filter(pl.col("symbol") == symbol)
        compared = raw.select("date", "close", "adjusted").join(
            qfq.select("date", pl.col("close").alias("qfq_close")),
            on="date",
            how="full",
            coalesce=True,
        ).with_columns(
            (
                (pl.col("adjusted") - pl.col("qfq_close")).abs()
                / pl.col("qfq_close").abs()
            ).alias("rel_error")
        )

        missing = compared.filter(
            pl.col("adjusted").is_null() | pl.col("qfq_close").is_null()
        )
        if missing.height:
            failures.append(
                f"{symbol}: {missing.height} date(s) missing on one side, "
                f"e.g. {missing['date'].head(5).to_list()}"
            )

        values = compared.drop_nulls(["adjusted", "qfq_close"])
        if values.is_empty():
            failures.append(f"{symbol}: no comparable days")
            continue
        worst = values.sort("rel_error", descending=True).head(1).row(0, named=True)
        summary.append(
            f"{symbol}: max rel error {worst['rel_error']:.3e} on {worst['date']}"
        )
        bad = values.filter(pl.col("rel_error") > QFQ_REL_TOL)
        if bad.height:
            worst_dates = (
                bad.sort("rel_error", descending=True)
                .head(10)
                .select("date", "rel_error")
                .to_dicts()
            )
            failures.append(
                f"{symbol}: max rel error {worst['rel_error']:.3e} on {worst['date']} "
                f"({bad.height} day(s) > {QFQ_REL_TOL:g}); worst: {worst_dates}"
            )

    if failures:
        pytest.fail(
            "raw→QFQ reproduction failed (" + "; ".join(summary) + "):\n"
            + "\n".join(failures)
        )


def test_live_rehab_matches_derived(
    derived_events: pl.DataFrame,
) -> None:
    derived: dict[str, dict] = {}
    for row in derived_events.to_dicts():
        derived.setdefault(row["symbol"], {})[row["trade_date"]] = row["ex_factor"]

    failures: list[str] = []
    with open_ctx() as ctx:
        for symbol in SYMBOLS:
            ret, data = _unpack_ret(ctx.get_rehab(to_moomoo(symbol)))
            if ret != 0:
                failures.append(f"{symbol}: get_rehab failed ret={ret!r} data={data!r}")
                continue
            rehab = {
                row["trade_date"]: row["ex_factor"]
                for row in rehab_events(symbol, _basicinfo_rows(data))
            }
            raw_qfq = derived.get(symbol, {})
            missing = sorted(set(raw_qfq) - set(rehab))
            if missing:
                failures.append(f"{symbol}: derived event dates absent from rehab: {missing}")
            for day in sorted(set(raw_qfq) & set(rehab)):
                if not math.isclose(raw_qfq[day], rehab[day], rel_tol=REHAB_REL_TOL):
                    failures.append(
                        f"{symbol} {day}: derived ex_factor={raw_qfq[day]!r} "
                        f"rehab ex_factor={rehab[day]!r}"
                    )

    if failures:
        pytest.fail("rehab vs raw/QFQ-derived events failed:\n" + "\n".join(failures))


def test_live_suspension_values() -> None:
    import moomoo

    with open_ctx() as ctx:
        ret, data = _unpack_ret(
            ctx.get_stock_basicinfo(moomoo.Market.US, moomoo.SecurityType.STOCK)
        )
    assert ret == 0, f"get_stock_basicinfo failed: ret={ret!r} data={data!r}"

    rows = _basicinfo_rows(data)
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("suspension"))
        counts[key] = counts.get(key, 0) + 1

    print(
        f"\nlive suspension values ({len(rows)} US stock rows): "
        f"{dict(sorted(counts.items()))}"
    )
    assert rows, "get_stock_basicinfo returned no rows"