"""Task 6 contract: ``get_rehab`` rows → event rows, and ``get_adj_factors``.

No test touches a real OpenD: ``FakeQuoteContext`` answers ``get_rehab``, and
the per-day event cache is written into ``tmp_path``.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_clock import us_clock
from app.plugins.moomoo.rehab import rehab_events
from tests.moomoo_fakes import FakeClock, FakeQuoteContext

ADJ_SCHEMA = {
    "symbol": pl.Utf8,
    "trade_date": pl.Date,
    "ex_factor": pl.Float64,
    "vol_factor": pl.Float64,
}


# ── rehab row mapping ────────────────────────────────────────
def test_rehab_split_nvda() -> None:
    events = rehab_events(
        "NVDA",
        [
            {
                "ex_div_date": "2024-06-10",
                "split_base": 1,
                "split_ert": 10,
                "split_ratio": 0.1,
                "forward_adj_factorA": 0.1,
                "forward_adj_factorB": 0,
            }
        ],
    )

    assert events == [
        {
            "symbol": "NVDA",
            "trade_date": dt.date(2024, 6, 10),
            "ex_factor": 10.0,
            "vol_factor": 10.0,
        }
    ]


def test_rehab_dividend_aapl() -> None:
    events = rehab_events(
        "AAPL",
        [
            {
                "ex_div_date": "2026-11-12",
                "per_cash_div": 0.27,
                "split_ratio": float("nan"),
                "forward_adj_factorA": 0.99924,
                "forward_adj_factorB": 0,
            }
        ],
    )

    assert len(events) == 1
    assert events[0]["trade_date"] == dt.date(2026, 11, 12)
    assert events[0]["ex_factor"] == pytest.approx(1 / 0.99924)
    assert events[0]["vol_factor"] == 1.0


def test_rehab_same_date_rows_multiply() -> None:
    events = rehab_events(
        "KO",
        [
            {
                "ex_div_date": "2026-03-02",
                "forward_adj_factorA": 0.999,
                "forward_adj_factorB": 0,
            },
            {
                "ex_div_date": "2026-03-02",
                "split_ratio": 0.5,
                "split_base": 2,
                "forward_adj_factorA": 0.5,
                "forward_adj_factorB": 0,
            },
        ],
    )

    assert len(events) == 1
    assert events[0]["ex_factor"] == pytest.approx((1 / 0.999) * (1 / 0.5))
    assert events[0]["vol_factor"] == pytest.approx(2.0)


def test_rehab_b_nonzero_rejected() -> None:
    with pytest.raises(ValueError):
        rehab_events(
            "BAD",
            [
                {
                    "ex_div_date": "2026-03-02",
                    "forward_adj_factorA": 0.9,
                    "forward_adj_factorB": 0.01,
                }
            ],
        )


def test_rehab_split_without_ratio_rejected() -> None:
    with pytest.raises(ValueError):
        rehab_events(
            "BAD",
            [
                {
                    "ex_div_date": "2026-03-02",
                    "split_base": 1,
                    "split_ert": 10,
                    "forward_adj_factorA": 0.1,
                    "forward_adj_factorB": 0,
                }
            ],
        )


# ── provider.get_adj_factors: cache first, rehab fallback ────
def _bars_frame(code: str, closes: list[float]) -> pl.DataFrame:
    days = [dt.date(2026, 11, 12) + dt.timedelta(days=i) for i in range(len(closes))]
    return pl.DataFrame(
        {
            "time_key": [f"{day.isoformat()} 00:00:00" for day in days],
            "close": closes,
            "volume": [1000.0] * len(days),
            "code": [code] * len(days),
        }
    )


def _rehab(rows_by_code: dict[str, list[dict]]) -> object:
    def _call(*args: object, **_kwargs: object) -> object:
        return 0, rows_by_code[str(args[0])]

    return _call


def _provider(tmp_path, rows_by_code: dict[str, list[dict]]):
    from app.plugins.moomoo import provider as provider_module

    clock = FakeClock()
    ctx = FakeQuoteContext(clock=clock, returns={"get_rehab": _rehab(rows_by_code)})
    provider_module.set_ctx_factory(lambda host, port: ctx)
    provider = provider_module.MoomooProvider()
    provider._clock = clock.monotonic
    provider._sleep = clock.sleep
    return provider, ctx, provider_module


@pytest.fixture(autouse=True)
def _reset_ctx_factory():
    from app.plugins.moomoo import provider as provider_module

    yield
    provider_module.set_ctx_factory(None)


def test_adj_uses_cache_then_rehab(tmp_path) -> None:
    from app.plugins.moomoo import events_cache

    events_cache.write(
        tmp_path,
        us_clock.today(),
        [
            {
                "symbol": "AAPL",
                "trade_date": dt.date(2026, 11, 12),
                "ex_factor": 1.0008,
                "vol_factor": 1.0,
            }
        ],
        {"AAPL"},
    )
    provider, ctx, provider_module = _provider(
        tmp_path,
        {
            "US.NVDA": [
                {
                    "ex_div_date": "2024-06-10",
                    "split_base": 1,
                    "split_ert": 10,
                    "split_ratio": 0.1,
                    "forward_adj_factorA": 0.1,
                    "forward_adj_factorB": 0,
                }
            ]
        },
    )
    provider_module.settings = SimpleNamespace(data_dir=tmp_path)

    frame = provider.get_adj_factors(
        ["AAPL", "NVDA"],
        start_time=dt.datetime(2024, 1, 1),
        end_time=dt.datetime(2026, 11, 13),
    )

    calls = ctx.calls_named("get_rehab")
    assert len(calls) == 1
    assert calls[0]["args"][0] == "US.NVDA"
    assert frame.schema == ADJ_SCHEMA
    assert frame["symbol"].to_list() == ["AAPL", "NVDA"]
    assert frame["trade_date"].to_list() == [dt.date(2026, 11, 12), dt.date(2024, 6, 10)]
    nvda = frame.filter(pl.col("symbol") == "NVDA")
    assert nvda["ex_factor"].to_list() == [10.0]
    assert nvda["vol_factor"].to_list() == [10.0]


def test_rehab_rate_limit_backs_off_and_retries(tmp_path) -> None:
    """OpenD's "high frequency" reply must not abort the whole sync: wait, retry once."""
    from app.plugins.moomoo.provider import RATE_LIMIT_BACKOFF_S

    replies = [
        (-1, "Get Adjusted Factor ... request failed due to high frequency. Maximum 60 times per 30 seconds."),
        (0, [
            {
                "ex_div_date": "2024-06-10",
                "split_base": 1,
                "split_ert": 10,
                "split_ratio": 0.1,
                "forward_adj_factorA": 0.1,
                "forward_adj_factorB": 0,
            }
        ]),
    ]

    def _call(*_args: object, **_kwargs: object) -> object:
        return replies.pop(0)

    from app.plugins.moomoo import provider as provider_module

    clock = FakeClock()
    ctx = FakeQuoteContext(clock=clock, returns={"get_rehab": _call})
    provider_module.set_ctx_factory(lambda host, port: ctx)
    provider = provider_module.MoomooProvider()
    provider._clock = clock.monotonic
    sleeps: list[float] = []

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.sleep(seconds)

    provider._sleep = _sleep
    provider_module.settings = SimpleNamespace(data_dir=tmp_path)

    frame = provider.get_adj_factors(
        ["NVDA"], start_time=dt.datetime(2024, 1, 1), end_time=dt.datetime(2026, 11, 13)
    )

    assert len(ctx.calls_named("get_rehab")) == 2
    assert RATE_LIMIT_BACKOFF_S in sleeps
    assert frame["ex_factor"].to_list() == [10.0]


def test_rehab_pacer_has_margin() -> None:
    from app.plugins.moomoo.provider import REHAB_CALLS_PER_WINDOW, SNAPSHOT_RPM

    assert REHAB_CALLS_PER_WINDOW <= 50
    assert SNAPSHOT_RPM <= 50


def test_adj_bad_symbol_skipped_others_kept(tmp_path) -> None:
    provider, _ctx, provider_module = _provider(
        tmp_path,
        {
            "US.AAPL": [
                {
                    "ex_div_date": "2026-11-12",
                    "forward_adj_factorA": 0.999,
                    "forward_adj_factorB": 0.01,
                }
            ],
            "US.NVDA": [
                {
                    "ex_div_date": "2024-06-10",
                    "split_ratio": 0.1,
                    "split_base": 1,
                    "forward_adj_factorA": 0.1,
                    "forward_adj_factorB": 0,
                }
            ],
        },
    )
    provider_module.settings = SimpleNamespace(data_dir=tmp_path)

    frame = provider.get_adj_factors(["AAPL", "NVDA"])

    assert frame.schema == ADJ_SCHEMA
    assert frame["symbol"].to_list() == ["NVDA"]
    assert frame["trade_date"].to_list() == [dt.date(2024, 6, 10)]


def test_invalid_qfq_symbol_not_covered_falls_back_to_rehab(tmp_path) -> None:
    from app.plugins.moomoo import events_cache

    provider, ctx, provider_module = _provider(
        tmp_path,
        {
            "US.BAD": [
                {
                    "ex_div_date": "2024-06-10",
                    "split_ratio": 0.1,
                    "split_base": 1,
                    "forward_adj_factorA": 0.1,
                    "forward_adj_factorB": 0,
                }
            ]
        },
    )
    provider_module.settings = SimpleNamespace(data_dir=tmp_path)

    good = _bars_frame("US.AAPL", [100.0, 101.0])
    provider._write_events_cache(
        {
            "US.AAPL": (good, good),
            # Negative QFQ close: events_for_symbols rejects this symbol.
            "US.BAD": (
                _bars_frame("US.BAD", [100.0, 101.0]),
                _bars_frame("US.BAD", [100.0, -1.0]),
            ),
        }
    )

    cached = events_cache.read(tmp_path, us_clock.today())
    assert cached is not None
    _events, covered = cached
    assert covered == {"AAPL"}

    provider.get_adj_factors(["AAPL", "BAD"])

    calls = ctx.calls_named("get_rehab")
    assert [call["args"][0] for call in calls] == ["US.BAD"]
