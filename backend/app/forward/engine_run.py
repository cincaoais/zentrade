"""Morning engine run: let the research engine decide from the real opening price.

M2 of the design runs the unchanged research backtest over the forward store
through day ``t`` plus an *open-only* bar for day ``t+1``. The engine's actions
dated ``t+1`` become the orders; its ``end`` sales are an artefact of ending the
run and never become orders.

The engine never enters on the last bar of its matrix (``app/backtest/engine.py:2104``
gates entries with ``if time_id < time_count - 1``), so the input also gets a
*PAD* bar dated the next trading day after ``t+1`` with the same OHLCV values as
the open-only bar. ``t+1`` is then a normal fill bar: an entry dated ``t`` buys
at ``t+1``'s open and is end-sold on the pad day, and ``actions_on(result, t1)``
keeps that buy while ignoring anything dated on the pad day.

Step 1 discovery (2026-11-18, reading the pinned engine code)
-------------------------------------------------------------
What the engine reads under the data directory for ``research_signal_table``
(matrix-native, ``mode="position"``):

* ``kline_daily_enriched/date=*/part.parquet`` — the market matrix. The path is
  ``store.data_dir / enriched_dirname(asset_type)`` at
  ``app/backtest/engine.py:564`` and ``enriched_dirname("stock")`` returns
  ``kline_daily_enriched`` at ``app/storage/repository.py:67-69``. Only
  ``symbol, date, open, high, low, close, volume`` are mandatory
  (``app/backtest/matrix.py:811-816``).
* ``instruments/**/*.parquet`` — names and the instrument fields used by the
  matrix fingerprint (``app/backtest/engine.py:565``, glob at
  ``app/storage/repository.py:369``, weak-cache load at
  ``app/storage/repository.py:1060-1066``).
* ``.matrix_generation_stock.json`` — the enriched generation marker read (and
  written when absent) by ``BacktestEngine.data_generation_await``
  (``app/backtest/engine.py:588-591`` ->
  ``app/storage/repository.py:1954-1956`` ->
  ``app/enriched_generation.py:28-29, 196-222``).
* ``.backtest_matrix_cache/`` — the shared matrix disk cache. The engine roots it
  at ``store.data_dir / ".backtest_matrix_cache"`` when
  ``settings.backtest_matrix_disk_cache_enabled`` is true
  (``app/backtest/engine.py:571-576``, ``app/config.py:118``) and creates/writes
  it at ``app/backtest/matrix.py:717-718, 757``. **``run_engine`` disables the
  disk cache for the duration of the call** so a run can never write a cache
  under the source data dir; the matrix is built in memory.
* ``user_data/custom_factors/*.json`` — custom/composite factors registered by
  ``build_service`` (``app/factors/store.py:29-33, 174-196``); missing is fine.
* ``user_data/strategy_overrides/<id>.json`` — per-strategy overrides
  (``app/strategy/config.py:32-34, 47``); missing is fine.
* ``strategies/{custom,ai,composite}/*.py`` — extra strategy directories, as
  ``scripts/research/evaluate.py:71-77`` passes them. ``research_signal_table``
  itself lives in the repo's ``app/strategy/builtin``.
* The benchmark curve reads SPY from the enriched store through
  ``repo.get_daily`` (``app/backtest/strategy.py:2194-2200`` ->
  ``app/storage/repository.py:1744-1759``). A store without SPY still trades;
  it only returns an empty benchmark curve.

``build_engine_store`` copies the market partitions it needs plus
``instruments``, ``.matrix_generation_stock.json`` and, when present,
``user_data``/``strategies``. It never writes ``src_data_dir``.

Tradability on the open-only bar: ``matrix.tradable`` is
``_tradable_matrix`` (``app/backtest/matrix.py:2580-2599``): a row is tradable
when the OHLCV values are present, at least one price is positive and the row
is not a zero-volume flat bar. The open-only row copies day ``t``'s volume and
sets ``open = high = low = close`` to the official open, so a real open with
normal volume is tradable even though the bar has no intraday range, while a
symbol with no open has no row at all and the engine treats it as not tradable
(``app/backtest/engine.py:1882-1893`` blocks the buy/sell and
``app/backtest/engine.py:2047-2049`` skips the intraday stop).

BLOCKER found while writing the tests (2026-11-18): the engine never enters on
the last bar of its matrix. ``_simulate_portfolio_matrix`` gates the entry block
with ``if time_id < time_count - 1 and max_positions > 0:``
(``app/backtest/engine.py:2104``), so a table entry dated ``t`` is delayed to
the open-only bar ``t+1`` and then dropped because ``t+1`` is the final row.
Fixture proof: with a 60-bar store and a table entry on bar 58, ``run_engine``
returns no trades; an entry on bar 57 fills on bar 58 and is end-sold on bar 59.
With the design's store-through-``t`` plus one open-only bar, ``actions_on(t1)``
can therefore only ever return sells, which contradicts Task 6 Step 2
(``test_entry_on_t_needs_open_only_bar``) and the buy half of M2/section 6.4.
A fix needs a design change (an extra bar after ``t+1``, a different engine
entry point, or editing the pinned engine), not a forward-package workaround.

RESOLVED by the coordinator's decision: the store gets TWO extra rows per
symbol, the open-only ``t+1`` row and an identical PAD row dated the next
trading day after ``t+1`` (``next_pad_day``). ``run_engine`` ends on the pad day,
so ``t+1`` is no longer final and its entries execute; the pad day's ``end``
sales are ignored by ``actions_on``.
"""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

import polars as pl

from app.backtest.strategy import StrategyBacktestConfig
from app.config import settings
from app.forward.tables import kept_entry_symbols
from app.market_clock import us_clock

STRATEGY_ID = "research_signal_table"
DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_MAX_POSITIONS = 10
BUILTIN_STRATEGY_DIR = Path(__file__).resolve().parents[1] / "strategy" / "builtin"
_COPY_DIRS = ("instruments", "user_data", "strategies")
_GENERATION_MARKER = ".matrix_generation_stock.json"


class Action(NamedTuple):
    """One engine action for the morning of ``t+1``."""

    kind: str  # "buy" | "sell"
    symbol: str
    shares: int
    price: float
    reason: str
    signal_date: str


def next_pad_day(t1: date) -> date:
    """First trading day strictly after ``t1``.

    ``us_clock`` uses the NYSE calendar; when the calendar has no later day
    (fetch failed or a fencepost at the end of the range) fall back to one
    calendar day after ``t1``.
    """
    try:
        return us_clock.next_trading_day(t1)
    except ValueError:
        return t1 + timedelta(days=1)


def open_only_rows(
    bars_t: pl.DataFrame,
    opens: dict[str, float],
    t1: date,
    pad_day: date,
) -> pl.DataFrame:
    """Two rows per symbol with an official open: the ``t+1`` bar and its pad bar.

    Every enriched column is copied from the symbol's day-``t`` row, then
    ``date`` becomes ``t1`` and ``open = high = low = close = opens[symbol]``;
    volume and amount stay at the day-``t`` values so the engine does not mistake
    the bar for a suspension. A second, otherwise identical row is dated
    ``pad_day`` so the engine's last-bar entry guard does not swallow the buy on
    ``t1`` (``app/backtest/engine.py:2104``). A symbol missing from ``bars_t`` or
    without a usable open gets no rows.
    """
    if bars_t.is_empty() or not opens:
        return bars_t.head(0)
    if pad_day <= t1:
        raise ValueError(f"pad_day {pad_day} must be after t1 {t1}")

    rows_by_symbol = {
        str(row["symbol"]): row for row in bars_t.iter_rows(named=True)
    }
    rows: list[dict] = []
    for symbol in sorted(opens):
        row = rows_by_symbol.get(symbol)
        price = float(opens[symbol])
        if row is None or price <= 0 or price != price or price == float("inf"):
            continue
        enriched = dict(row)
        enriched.update(date=t1, open=price, high=price, low=price, close=price)
        rows.append(enriched)
        pad = dict(enriched)
        pad["date"] = pad_day
        rows.append(pad)
    if not rows:
        return bars_t.head(0)
    return pl.DataFrame(rows, schema=bars_t.schema)


def build_engine_store(
    src_data_dir: Path,
    dst: Path,
    symbols: list[str],
    start: date,
    end: date,
    extra_rows: pl.DataFrame | None,
) -> Path:
    """Build a private, read-only-for-the-engine store at ``dst``.

    Only the requested symbols and date range are copied from
    ``kline_daily_enriched/date=*/part.parquet``; ``extra_rows`` (the open-only
    ``t+1`` bars and their pad bars) are appended and win on a duplicate
    ``(symbol, date)``. Instruments, the enriched generation marker and the
    optional ``user_data``/``strategies`` trees are copied because the engine
    reads them. ``src_data_dir`` is only read, never written.
    """
    src = Path(src_data_dir)
    dst = Path(dst)
    wanted = [str(symbol) for symbol in symbols]

    frames: list[pl.DataFrame] = []
    # The main store first; the runner's own copy (``forward/bars``, written by
    # ``preserve_bars`` for held symbols) fills what the daily pipeline pruned.
    for root in (src / "kline_daily_enriched", src / "forward" / "bars"):
        if not any(root.glob("date=*/part.parquet")):
            continue
        frames.append(
            pl.scan_parquet(str(root / "date=*" / "part.parquet"), hive_partitioning=False)
            .filter(pl.col("symbol").is_in(wanted))
            .filter((pl.col("date") >= start) & (pl.col("date") <= end))
            .collect()
        )
    if not frames:
        raise FileNotFoundError(f"no daily bars under {src}")
    bars = frames[0] if len(frames) == 1 else pl.concat(frames, how="diagonal_relaxed")
    bars = bars.unique(subset=["symbol", "date"], keep="first", maintain_order=True).sort(
        ["symbol", "date"]
    )

    if extra_rows is not None and not extra_rows.is_empty():
        extra = extra_rows.filter(pl.col("symbol").is_in(wanted))
        if not extra.is_empty():
            bars = pl.concat([bars, extra], how="vertical_relaxed")
            bars = bars.unique(subset=["symbol", "date"], keep="last").sort(
                ["symbol", "date"]
            )

    enriched = dst / "kline_daily_enriched"
    if enriched.exists():
        shutil.rmtree(enriched)
    enriched.mkdir(parents=True, exist_ok=True)
    for key, part in bars.group_by(["date"], maintain_order=True):
        day = key[0] if isinstance(key, tuple) else key
        target = enriched / f"date={day.isoformat()}"
        target.mkdir(parents=True, exist_ok=True)
        part.write_parquet(target / "part.parquet")

    for name in _COPY_DIRS:
        source = src / name
        if source.is_dir():
            shutil.copytree(source, dst / name, dirs_exist_ok=True)
    marker = src / _GENERATION_MARKER
    if marker.is_file():
        shutil.copy2(marker, dst / marker.name)

    return dst


def build_service(store_dir: Path):
    """The research service builder (``scripts/research/evaluate._build_service``).

    Re-implemented here so ``app/forward`` does not import a script.
    """
    from app.backtest.engine import BacktestEngine
    from app.backtest.strategy import StrategyBacktestService
    from app.factors.store import load_into_registry
    from app.storage.repository import DataStore, KlineRepository
    from app.strategy import config as strategy_config
    from app.strategy.engine import StrategyEngine

    data_dir = Path(store_dir)
    load_into_registry(data_dir)
    strategy_engine = StrategyEngine(
        strategy_dirs=[
            BUILTIN_STRATEGY_DIR,
            data_dir / "strategies" / "custom",
            data_dir / "strategies" / "ai",
            data_dir / "strategies" / "composite",
        ],
        override_loader=lambda sid: strategy_config.load_override(data_dir, sid),
    )
    return StrategyBacktestService(
        BacktestEngine(KlineRepository(DataStore(data_dir))), strategy_engine
    )


def engine_config(
    table_path: Path,
    start: date,
    end: date,
    capital: float,
    *,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    max_positions: int = DEFAULT_MAX_POSITIONS,
) -> StrategyBacktestConfig:
    """The research config, field for field, plus ``initial_capital``.

    ``app.research.evaluate.run_table`` (``evaluate.py:122-143``) does not expose
    ``initial_capital``, so the forward runner builds the identical config here
    and adds the strategy's registered capital.
    """
    return StrategyBacktestConfig(
        strategy_id=STRATEGY_ID,
        symbols=None,
        start=start,
        end=end,
        params={"table_path": str(table_path)},
        max_positions=max_positions,
        position_sizing="equal",
        entry_fill="open_t+1",
        exit_fill="open_t+1",
        slippage_bps=slippage_bps,
        mode="position",
        initial_capital=float(capital),
    )


def run_engine(
    store_dir: Path,
    table: pl.DataFrame,
    start: date,
    end: date,
    capital: float,
) -> object:
    """Run the research engine over the private store and return its result.

    ``end`` must be the pad day (``next_pad_day(t1)``) so the open-only ``t1``
    bar is not the final matrix row; otherwise the engine's last-bar guard drops
    the entries it is supposed to fill there. The composed table is written
    inside ``store_dir`` (never the source store). The shared matrix disk cache
    is disabled for the call so nothing is written to
    ``.backtest_matrix_cache``.
    """
    store_dir = Path(store_dir)
    table_dir = store_dir / "forward"
    table_dir.mkdir(parents=True, exist_ok=True)
    table_path = table_dir / "decision_table.parquet"
    table.write_parquet(table_path)

    service = build_service(store_dir)
    config = engine_config(table_path, start, end, capital)

    previous_cache_setting = settings.backtest_matrix_disk_cache_enabled
    settings.backtest_matrix_disk_cache_enabled = False
    try:
        return service.run(config)
    finally:
        settings.backtest_matrix_disk_cache_enabled = previous_cache_setting


def _field(trade: object, name: str, default: object = None) -> object:
    if isinstance(trade, dict):
        return trade.get(name, default)
    return getattr(trade, name, default)


def _date_text(value: object) -> str:
    return "" if value is None else str(value)[:10]


def actions_on(result: object, t1: date) -> list[Action]:
    """The engine's buys and sells dated ``t1``, sells first.

    A trade with ``entry_date == t1`` is a buy; a trade with ``exit_date == t1``
    is a sell. ``exit_reason == "end"`` is the engine closing every position on
    the final bar of the run, so those sales are dropped: they are not
    decisions. The pad day exists only to keep the engine running past ``t1``;
    the ``end`` sale of a ``t1`` buy lands there and is ignored, while the buy
    itself is kept.
    """
    target = t1.isoformat()
    sells: list[Action] = []
    buys: list[Action] = []
    for trade in getattr(result, "trades", None) or []:
        symbol = str(_field(trade, "symbol", ""))
        if _date_text(_field(trade, "exit_date")) == target:
            reason = str(_field(trade, "exit_reason", ""))
            if reason != "end":
                sells.append(
                    Action(
                        kind="sell",
                        symbol=symbol,
                        shares=round(float(_field(trade, "shares", 0.0) or 0.0)),
                        price=float(_field(trade, "exit_price", 0.0) or 0.0),
                        reason=reason,
                        signal_date=_date_text(_field(trade, "exit_signal_date")),
                    )
                )
        if _date_text(_field(trade, "entry_date")) == target:
            buys.append(
                Action(
                    kind="buy",
                    symbol=symbol,
                    shares=round(float(_field(trade, "shares", 0.0) or 0.0)),
                    price=float(_field(trade, "entry_price", 0.0) or 0.0),
                    reason="entry",
                    signal_date=_date_text(_field(trade, "entry_signal_date")),
                )
            )
    return sells + buys


def engine_holdings(result: object, t1: date) -> set[str]:
    """Symbols the engine still holds after the session of ``t1``.

    A trade entered on or before ``t1`` that exits after it, or is only closed
    by the run's final ``end`` sale, is held. A position the engine could never
    sell (a stock that stopped trading) is not in ``trades`` at all, so it is
    not reported here either.
    """
    target = t1.isoformat()
    held: set[str] = set()
    for trade in getattr(result, "trades", None) or []:
        entry = _date_text(_field(trade, "entry_date"))
        exit_ = _date_text(_field(trade, "exit_date"))
        reason = str(_field(trade, "exit_reason", ""))
        if entry and entry <= target and (not exit_ or exit_ > target or reason == "end"):
            held.add(str(_field(trade, "symbol", "")))
    return held


def relevant_symbols(
    table: pl.DataFrame, start: date, held: set[str], candidates: list[str]
) -> list[str]:
    """Symbols the engine may need: kept entries since ``start``, held names, and
    today's candidates.

    ``candidates`` must come from ``app.forward.tables.candidates_on`` (raw
    signals + eligibility): at night the composed table has no day-``t`` entries
    because compose drops them for lack of a next bar.
    """
    symbols = set(kept_entry_symbols(table, start)) | set(held) | set(candidates)
    return sorted(str(symbol) for symbol in symbols)


__all__ = [
    "Action",
    "actions_on",
    "build_engine_store",
    "build_service",
    "engine_config",
    "engine_holdings",
    "next_pad_day",
    "open_only_rows",
    "relevant_symbols",
    "run_engine",
]