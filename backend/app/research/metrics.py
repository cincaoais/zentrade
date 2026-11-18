"""Trade R, curve statistics and the deflated Sharpe ratio.

All Sharpe ratios passed to :func:`psr` and :func:`dsr` are per-period
(daily), never annualised.  The normal distribution comes from the standard
library (``statistics.NormalDist``); no SciPy dependency is used.

References:
    Bailey, D. H. and Lopez de Prado, M. (2014), "The Deflated Sharpe Ratio:
    Correcting for Selection Bias, Backtest Overfitting and Non-Normality",
    Journal of Portfolio Management 40(5), 94-107.  SSRN 2460551.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime
from itertools import pairwise
from statistics import NormalDist

import polars as pl

logger = logging.getLogger(__name__)

# The engine runs an equal-weight, ten-slot portfolio (Global Constraints).
MAX_POSITIONS = 10
TRADING_DAYS = 252
EULER_GAMMA = 0.5772156649


def _field(record: object, name: str) -> object | None:
    """Read ``name`` from a trade dict or from a dataclass/object."""
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def _as_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_iso(value: object) -> str | None:
    parsed = _as_date(value)
    return parsed.isoformat() if parsed is not None else None


def trade_r(trades: list[dict], table: pl.DataFrame) -> list[float]:
    """Return ``(exit - entry) / max(entry - invalidation, 0.5 * atr14)``.

    Each trade is joined on ``(symbol, entry_signal_date)`` to the table's
    entry row.  Trades without a matching entry row (or without a usable
    risk denominator) are skipped and counted in the logger.
    """
    if not trades:
        return []

    rows = table
    if "entry" in table.columns:
        rows = rows.filter(pl.col("entry").fill_null(False))

    lookup: dict[tuple[str, str | None], dict] = {}
    for row in rows.iter_rows(named=True):
        symbol = row.get("symbol")
        signal_date = _as_iso(row.get("date"))
        if symbol is None or signal_date is None:
            continue
        lookup[(str(symbol), signal_date)] = row

    results: list[float] = []
    skipped = 0
    for trade in trades:
        symbol = _field(trade, "symbol")
        signal_date = _as_iso(_field(trade, "entry_signal_date"))
        row = lookup.get((str(symbol), signal_date)) if symbol is not None else None
        if row is None:
            skipped += 1
            continue

        entry_price = _field(trade, "entry_price")
        exit_price = _field(trade, "exit_price")
        invalidation = row.get("invalidation")
        atr14 = row.get("atr14")
        if entry_price is None or exit_price is None or invalidation is None:
            skipped += 1
            continue

        entry_price = float(entry_price)
        exit_price = float(exit_price)
        atr_floor = 0.5 * float(atr14) if atr14 is not None else 0.0
        denominator = max(entry_price - float(invalidation), atr_floor)
        if not math.isfinite(denominator) or denominator <= 0.0:
            skipped += 1
            continue

        results.append((exit_price - entry_price) / denominator)

    if skipped:
        logger.info(
            "trade_r skipped %d of %d trades (unmatched entry row or bad risk floor)",
            skipped,
            len(trades),
        )
    return results


def _equity_values(equity: pl.DataFrame) -> list[float]:
    column = "equity" if "equity" in equity.columns else "value"
    if column not in equity.columns:
        return []
    return [
        float(value)
        for value in equity[column].cast(pl.Float64).to_list()
        if value is not None and math.isfinite(float(value))
    ]


def _daily_returns(values: list[float]) -> list[float]:
    returns = []
    for previous, current in pairwise(values):
        if previous != 0.0:
            returns.append(current / previous - 1.0)
    return returns


def _moments(values: list[float]) -> tuple[float, float]:
    """Return population skewness and non-excess kurtosis (normal = 3)."""
    n = len(values)
    if n < 2:
        return 0.0, 3.0
    mean = sum(values) / n
    centred = [value - mean for value in values]
    variance = sum(value * value for value in centred) / n
    if variance <= 0.0:
        return 0.0, 3.0
    skew = (sum(value**3 for value in centred) / n) / variance**1.5
    kurt = (sum(value**4 for value in centred) / n) / variance**2
    return float(skew), float(kurt)


def curve_stats(equity: pl.DataFrame) -> dict:
    """CAGR, daily/annualised Sharpe, MDD and return moments.

    ``equity`` has a ``date`` column plus ``equity`` (or the engine's
    ``value``) and is expected to be one row per trading day.
    """
    values = _equity_values(equity)
    n_days = len(values)
    returns = _daily_returns(values)
    n_returns = len(returns)

    cagr = 0.0
    if n_returns > 0 and values[0] > 0.0 and values[-1] > 0.0:
        cagr = (values[-1] / values[0]) ** (TRADING_DAYS / n_returns) - 1.0

    sharpe_daily = 0.0
    if n_returns > 1:
        mean = sum(returns) / n_returns
        variance = sum((value - mean) ** 2 for value in returns) / (n_returns - 1)
        if variance > 0.0:
            sharpe_daily = mean / math.sqrt(variance)

    max_drawdown = 0.0
    peak = None
    for value in values:
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0.0:
            max_drawdown = min(max_drawdown, value / peak - 1.0)

    skew, kurt = _moments(returns)

    return {
        "cagr": float(cagr),
        "sharpe_ann": float(sharpe_daily * math.sqrt(TRADING_DAYS)),
        "sharpe_daily": float(sharpe_daily),
        "max_drawdown": float(max_drawdown),
        "n_days": int(n_days),
        "skew": skew,
        "kurtosis": kurt,
    }


def exposure(trades: list[dict], n_days: int, slots: int = MAX_POSITIONS) -> float:
    """Average fraction of the ``slots`` portfolio slots held per day."""
    if n_days <= 0:
        return 0.0

    position_days = 0.0
    for trade in trades:
        duration = _field(trade, "duration")
        if duration is not None:
            position_days += max(0.0, float(duration))
            continue
        entry = _as_date(_field(trade, "entry_date"))
        exit_date = _as_date(_field(trade, "exit_date"))
        if entry is None or exit_date is None:
            continue
        position_days += max(0, (exit_date - entry).days + 1)

    return position_days / (float(n_days) * slots)


def psr(sr: float, sr_ref: float, n: int, skew: float, kurt: float) -> float:
    """Probabilistic Sharpe ratio (Bailey & Lopez de Prado, Eq. 2 form)."""
    if n <= 1:
        return 0.5
    variance = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if variance <= 0.0:
        return float("nan")
    z = (sr - sr_ref) * math.sqrt(n - 1) / math.sqrt(variance)
    return float(NormalDist().cdf(z))


def expected_max_sr(var_sr: float, n_trials: int) -> float:
    """Expected maximum Sharpe ratio under the null across independent trials."""
    if n_trials <= 1 or var_sr <= 0.0:
        return 0.0
    normal = NormalDist()
    first = normal.inv_cdf(1.0 - 1.0 / n_trials)
    second = normal.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return float(
        math.sqrt(var_sr) * ((1.0 - EULER_GAMMA) * first + EULER_GAMMA * second)
    )


def dsr(
    sr: float,
    n: int,
    skew: float,
    kurt: float,
    var_sr_trials: float,
    n_trials: int,
) -> float:
    """Deflated Sharpe ratio; with one trial it reduces to PSR against zero."""
    sr_ref = 0.0 if n_trials <= 1 else expected_max_sr(var_sr_trials, n_trials)
    return psr(sr, sr_ref, n, skew, kurt)


def summarize(
    trades: list[dict],
    equity: pl.DataFrame,
    table: pl.DataFrame,
    n_trials: int,
    var_sr_trials: float,
    slots: int = MAX_POSITIONS,
) -> dict:
    """Bundle trade R, curve statistics, exposure and DSR for one variant."""
    r_values = trade_r(trades, table)
    stats = curve_stats(equity)
    n_trades = len(trades)

    win_rate = None
    avg_r = None
    median_r = None
    if r_values:
        win_rate = sum(1 for value in r_values if value > 0.0) / len(r_values)
        avg_r = sum(r_values) / len(r_values)
        ordered = sorted(r_values)
        middle = len(ordered) // 2
        median_r = (
            ordered[middle]
            if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / 2.0
        )

    n_returns = max(stats["n_days"] - 1, 1)
    if stats["n_days"] >= 2:
        dsr_value = dsr(
            stats["sharpe_daily"],
            n_returns,
            stats["skew"],
            stats["kurtosis"],
            var_sr_trials,
            n_trials,
        )
    else:
        dsr_value = None

    result = {
        "trades": n_trades,
        "win_rate": win_rate,
        "avg_r": avg_r,
        "median_r": median_r,
        "cagr": stats["cagr"],
        "sharpe_ann": stats["sharpe_ann"],
        "max_drawdown": stats["max_drawdown"],
        "exposure": exposure(trades, stats["n_days"], slots),
        "dsr": dsr_value,
    }
    if n_trades == 0:
        result["reason"] = "no trades"
    return result