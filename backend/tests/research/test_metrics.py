"""Trade R, curve statistics and the deflated Sharpe ratio."""

from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import NormalDist

import polars as pl
import pytest

from app.research.metrics import (
    dsr,
    expected_max_sr,
    psr,
    summarize,
    trade_r,
)


def _empty_table() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "date": pl.Date,
            "entry": pl.Boolean,
            "exit": pl.Boolean,
            "score": pl.Float64,
            "invalidation": pl.Float64,
            "atr14": pl.Float64,
            "close": pl.Float64,
        }
    )


def test_psr_formula_hand_value() -> None:
    expected = NormalDist().cdf(0.1 * math.sqrt(250) / math.sqrt(1 + 0.5 * 0.01))
    assert psr(0.1, 0.0, 251, 0.0, 3.0) == pytest.approx(expected, abs=1e-12)


def test_expected_max_sr_monotonic_in_trials() -> None:
    values = [expected_max_sr(0.01, n) for n in (2, 5, 10, 18, 100, 1000)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_dsr_published_example() -> None:
    """Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio", section 6.

    Source: https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf
    (SSRN 2460551).  The analyst reports N=100 independent trials,
    V[{SR_n}]=1/2 annualised, T=1250 daily observations (5 years at 250/year),
    skewness=-3 and kurtosis=10, with an annualised SR of 2.5.  The paper's
    rejection threshold is SR_0 ~= 0.1132 (daily, from sqrt(1/(2*250))) and
    its stated DSR is ~= 0.9004.
    """
    n_trials = 100
    n_obs = 1250
    annual_sr = 2.5
    skew = -3.0
    kurt = 10.0
    observations_per_year = 250

    # V[{SR_n}] = 1/2 is annualised; the paper deflates with sqrt(1/(2*250)).
    var_sr_trials = 0.5 / observations_per_year
    sr_daily = annual_sr / math.sqrt(observations_per_year)

    assert expected_max_sr(var_sr_trials, n_trials) == pytest.approx(0.1132, abs=5e-4)
    assert dsr(sr_daily, n_obs, skew, kurt, var_sr_trials, n_trials) == pytest.approx(
        0.9004, abs=0.005
    )


def test_metrics_zero_trades() -> None:
    start = date(2024, 1, 2)
    flat_equity = pl.DataFrame(
        {
            "date": [start + timedelta(days=i) for i in range(11)],
            "equity": [1_000_000.0] * 11,
        }
    )
    result = summarize([], flat_equity, _empty_table(), 18, 0.01)
    assert result["trades"] == 0
    assert result["avg_r"] is None
    assert result["reason"] == "no trades"


def test_trade_r_floor() -> None:
    table = pl.DataFrame(
        {
            "symbol": ["AAA"],
            "date": [date(2024, 1, 2)],
            "entry": [True],
            "exit": [False],
            "score": [0.5],
            "invalidation": [99.9],
            "atr14": [2.0],
            "close": [100.0],
        },
        schema_overrides={
            "date": pl.Date,
            "entry": pl.Boolean,
            "exit": pl.Boolean,
            "invalidation": pl.Float64,
            "atr14": pl.Float64,
            "close": pl.Float64,
        },
    )
    trades = [
        {
            "symbol": "AAA",
            "entry_signal_date": date(2024, 1, 2),
            "entry_price": 100.0,
            "exit_price": 101.0,
        }
    ]
    # Risk denominator: max(100 - 99.9, 0.5 * 2) = 1.0.
    assert trade_r(trades, table) == pytest.approx([1.0])