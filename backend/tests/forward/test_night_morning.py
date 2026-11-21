"""Night preparation and morning execution, entirely offline.

A fixture store, a fake trade context, fake quotes and a fake snapshot function
stand in for the real data dir, OpenD and the network. All records live under
``tmp_path``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest
from moomoo import TrdSide

from app.forward import config, morning, night, registration, tables
from app.forward.broker import SimBroker
from app.forward.data import load_forward_bars
from app.forward.days import record, statuses_on
from app.forward.engine_run import next_pad_day
from app.forward.morning import engine_entry_prices, run_morning
from app.forward.night import run_night
from app.forward.orders import Executor, Journal
from app.research.compose import Signals
from app.research.universe import UniverseParams
from tests.forward.fake_trade import FakeTradeContext

ET = ZoneInfo("America/New_York")

# 60 weekdays; T is Thursday, T1 Friday, so no weekly replay check on T.
DAYS: list[date] = []
_cursor = date(2025, 1, 6)
while len(DAYS) < 60:
    if _cursor.weekday() < 5:
        DAYS.append(_cursor)
    _cursor += timedelta(days=1)
T = DAYS[-2]
T1 = DAYS[-1]
PAD = next_pad_day(T1)
SYMBOLS = [f"S{index:02d}" for index in range(4)]
ALL_SYMBOLS = [*SYMBOLS, "SPY"]

_TABLE_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "entry": pl.Boolean,
    "exit": pl.Boolean,
    "score": pl.Float64,
}


# ── fixture data and repo ────────────────────────────────────────
def _price(symbol: str, index: int) -> float:
    base = 20.0 + 5.0 * SYMBOLS.index(symbol) if symbol in SYMBOLS else 100.0
    return base * (1.0 + 0.002 * index)


def _bars(symbol: str, days: list[date]) -> pl.DataFrame:
    rows = []
    for day in days:
        index = DAYS.index(day)
        price = _price(symbol, index)
        rows.append(
            {
                "symbol": symbol,
                "date": day,
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 1_000_000.0 + 1_000.0 * index,
                "amount": price * 1_000_000.0,
                "raw_close": price,
                "raw_high": price * 1.01,
                "raw_low": price * 0.99,
                # A non-canonical enriched column: the open-only template must
                # carry it or build_engine_store cannot concat the rows.
                "ma20": price,
            }
        )
    return pl.DataFrame(rows)


def _all_bars() -> pl.DataFrame:
    return pl.concat([_bars(symbol, DAYS) for symbol in ALL_SYMBOLS])


def _write_data(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    bars = _all_bars()
    for key, part in bars.group_by(["date"], maintain_order=True):
        day = key[0] if isinstance(key, tuple) else key
        target = root / "kline_daily_enriched" / f"date={day.isoformat()}"
        target.mkdir(parents=True, exist_ok=True)
        part.sort("symbol").write_parquet(target / "part.parquet")

    instruments = root / "instruments"
    instruments.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": ALL_SYMBOLS,
            "name": [f"Fixture {symbol}" for symbol in ALL_SYMBOLS],
            "asset_type": ["stock"] * len(ALL_SYMBOLS),
            "instrument_type": ["stock"] * len(ALL_SYMBOLS),
            "is_etf": [False] * len(ALL_SYMBOLS),
            "is_preferred": [False] * len(ALL_SYMBOLS),
            "is_warrant": [False] * len(ALL_SYMBOLS),
            "is_unit": [False] * len(ALL_SYMBOLS),
            "total_shares": [1_000_000_000.0] * len(ALL_SYMBOLS),
            "float_shares": [1_000_000_000.0] * len(ALL_SYMBOLS),
        }
    ).write_parquet(instruments / "instruments.parquet")

    (root / "research").mkdir(parents=True, exist_ok=True)
    (root / "research" / "registry.frozen.json").write_text(
        '{"sha256": "fixture"}\n', encoding="utf-8"
    )
    _write_snapshot(root, T)
    _write_snapshot(root, T1)
    return root


def _write_snapshot(root: Path, day: date) -> None:
    target = root / "forward" / "universe" / f"{day.isoformat()}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ALL_SYMBOLS}, schema={"symbol": pl.String}).write_parquet(
        target
    )


def _fake_repo(root: Path) -> Path:
    repo = root / "repo"
    for rel in config.PINNED_FILES:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# fake {rel}\n", encoding="utf-8", newline="\n")
    for rel in config.PINNED_TREES:
        nested = repo / rel / "sub" / "deep.py"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("# fake research file\n", encoding="utf-8", newline="\n")
    forward = repo / "backend" / "app" / "forward"
    forward.mkdir(parents=True, exist_ok=True)
    for name in ("__init__.py", "config.py", "night.py", "morning.py"):
        (forward / name).write_text(f"# fake {name}\n", encoding="utf-8", newline="\n")
    return repo


def _register(data: Path, repo: Path, *, start: date = DAYS[0]) -> dict:
    registration.write_registration(
        data,
        capital_per_strategy=250_000.0,
        start_date=start,
        account_id=123456,
        var_sr_trials=0.42,
        repo_root=repo,
    )
    return registration.load_registration(data)


@pytest.fixture
def world(tmp_path: Path) -> SimpleNamespace:
    data = _write_data(tmp_path / "data")
    repo = _fake_repo(tmp_path)
    reg = _register(data, repo)
    return SimpleNamespace(data=data, repo=repo, reg=reg, tmp=tmp_path)


# ── small helpers ────────────────────────────────────────────────
def _table(rows: list[tuple[str, date, bool, bool, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [row[0] for row in rows],
            "date": [row[1] for row in rows],
            "entry": [row[2] for row in rows],
            "exit": [row[3] for row in rows],
            "score": [row[4] for row in rows],
        },
        schema=_TABLE_SCHEMA,
    )


def _empty_table() -> pl.DataFrame:
    return _table([])


def _journal(data: Path, short: str) -> Journal:
    return Journal(data / "forward" / short / "orders.jsonl")


def _seed_lot(data: Path, short: str, symbol: str, shares: int, price: float) -> None:
    _journal(data, short).append(
        {
            "key": f"fw-{short}-20250101-{symbol}-B",
            "state": "filled",
            "symbol": symbol,
            "side": "B",
            "qty": shares,
            "dealt_qty": shares,
            "dealt_avg_price": price,
        }
    )


def _write_night(
    data: Path,
    short: str,
    *,
    ready: bool = True,
    relevant: list[str] | None = None,
    held: list[str] | None = None,
    candidates: list[str] | None = None,
    table_hash: str = "night-hash",
    reason: str = "",
) -> Path:
    path = data / "forward" / short / "night" / f"{T.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "date": T.isoformat(),
                "short": short,
                "ready": ready,
                "reason": reason,
                "relevant_symbols": relevant or [],
                "held": held or [],
                "candidates": candidates or [],
                "history_hashes": {},
                "table_hash": table_hash,
                "pins": {},
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    elig_path = data / "forward" / "night" / f"{T.isoformat()}-elig.parquet"
    elig_path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": list(ALL_SYMBOLS),
            "date": [T] * len(ALL_SYMBOLS),
            "eligible": [True] * len(ALL_SYMBOLS),
            "rank": [1] * len(ALL_SYMBOLS),
        },
        schema={
            "symbol": pl.String,
            "date": pl.Date,
            "eligible": pl.Boolean,
            "rank": pl.Int64,
        },
    ).write_parquet(elig_path)
    return path


def _fake_signals(
    entries: list[tuple[str, date, float]],
    sells: list[tuple[str, date]] | None = None,
) -> Signals:
    entries_frame = pl.DataFrame(
        {
            "symbol": [entry[0] for entry in entries],
            "date": [entry[1] for entry in entries],
            "invalidation": [entry[2] for entry in entries],
            "source": ["test"] * len(entries),
        },
        schema={
            "symbol": pl.String,
            "date": pl.Date,
            "invalidation": pl.Float64,
            "source": pl.String,
        },
    )
    sell_rows = sells or []
    sells_frame = pl.DataFrame(
        {
            "symbol": [entry[0] for entry in sell_rows],
            "date": [entry[1] for entry in sell_rows],
        },
        schema={"symbol": pl.String, "date": pl.Date},
    )
    return Signals(entries=entries_frame, sells=sells_frame)


def _reshape_s01_amounts(root: Path) -> None:
    """Make S01 rank 1 while it is held, then rank 4 on the signal day."""
    for path in sorted((root / "kline_daily_enriched").glob("date=*/part.parquet")):
        day = date.fromisoformat(path.parent.name.split("=", 1)[1])
        factor = 100.0 if DAYS.index(day) <= 30 else 0.9
        frame = pl.read_parquet(path)
        frame = frame.with_columns(
            pl.when(pl.col("symbol") == "S01")
            .then(pl.col("amount") * factor)
            .otherwise(pl.col("amount"))
            .alias("amount")
        )
        frame.write_parquet(path)


def _patch_point_loading(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    empty = pl.DataFrame()
    monkeypatch.setattr(module, "load_points_through", lambda data, t: (empty, empty, empty))


def _patch_morning_table(
    monkeypatch: pytest.MonkeyPatch, table: pl.DataFrame
) -> None:
    def fake_table(variant_id: str, inputs, elig, bars):
        if variant_id == "H2-spring":
            return table, {"kept": 1}
        return _empty_table(), {"kept": 0}

    monkeypatch.setattr(morning, "forward_table", fake_table)


class MarketClock:
    """Fake ET wall clock plus the monotonic face ``Executor`` needs."""

    def __init__(self, now: datetime) -> None:
        self._now = now
        self._monotonic = 1_000.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        seconds = max(0.0, float(seconds))
        self.sleeps.append(seconds)
        self._monotonic += seconds
        self._now += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)


class FakeQuotes:
    """Snapshots stamped just after T1's open unless ``times`` says otherwise."""

    def __init__(
        self,
        prices: dict[str, float | None],
        times: dict[str, float] | None = None,
    ) -> None:
        self.prices = prices
        self.times = dict(times or {})
        self.calls = 0

    def snapshot(self, symbols: list[str]) -> dict[str, dict]:
        self.calls += 1
        fresh = datetime.combine(T1, dt_time(9, 30, 5), tzinfo=ET).timestamp()
        return {
            symbol: {
                "open_price": self.prices.get(symbol),
                "last_price": self.prices.get(symbol),
                "update_time": self.times.get(symbol, fresh),
            }
            for symbol in symbols
        }


def _opens() -> dict[str, float]:
    return {
        symbol: _price(symbol, DAYS.index(T1)) for symbol in ALL_SYMBOLS
    }


def _morning_setup(world: SimpleNamespace, *, start_time: dt_time = dt_time(9, 31)):
    clock = MarketClock(datetime.combine(T1, start_time, tzinfo=ET))
    ctx = FakeTradeContext(
        account_id=123456,
        cash=250_000.0,
        positions={},
        clock=clock,
    )
    broker = SimBroker(123456, ctx_factory=lambda: ctx)
    quotes = FakeQuotes(_opens())

    def executor_factory(broker_arg, journal_arg, *, clock):
        return Executor(broker_arg, journal_arg, clock=clock)

    return clock, ctx, broker, quotes, executor_factory


def _run_morning(
    world: SimpleNamespace,
    *,
    clock,
    ctx,
    broker,
    quotes,
    executor_factory,
    dry_run: bool,
) -> dict:
    return run_morning(
        world.data,
        T1,
        broker=broker,
        quotes=quotes,
        executor_factory=executor_factory,
        clock=clock,
        dry_run=dry_run,
        repo_root=world.repo,
    )


def _order_calls(ctx: FakeTradeContext) -> list[dict]:
    return ctx.calls_named("place_order")


def _decision(world: SimpleNamespace, short: str = "h2s") -> dict:
    path = world.data / "forward" / short / "decision" / f"{T1.isoformat()}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ── night ───────────────────────────────────────────────────────
def test_night_writes_relevant_symbols_with_candidates(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_lot(world.data, "h2s", "S03", 10, _price("S03", 20))
    table = _table([("S01", DAYS[10], True, False, 1.0)])
    seen: dict[str, object] = {}

    def fake_extend(data, symbols, t, *, snapshot_fn=None, workers=1):
        seen["symbols"] = list(symbols)
        seen["snapshot_fn"] = snapshot_fn
        seen["workers"] = workers
        return {"extended": [], "replayed": [], "failed": []}

    monkeypatch.setattr(night, "extend_points", fake_extend)
    _patch_point_loading(monkeypatch, night)
    monkeypatch.setattr(night, "candidates_on", lambda entries, elig, t: ["S02"])
    monkeypatch.setattr(
        night, "forward_table", lambda variant, inputs, elig, bars: (table, {"kept": 1})
    )
    monkeypatch.setattr(
        night,
        "VARIANTS",
        {
            "H2-spring": lambda inputs: SimpleNamespace(entries=pl.DataFrame()),
            "H6-H4-3B": lambda inputs: SimpleNamespace(entries=pl.DataFrame()),
        },
    )

    result = run_night(
        world.data,
        T,
        repo_root=world.repo,
        snapshot_fn=lambda frame: {"status": "available"},
    )

    record_path = world.data / "forward" / "h2s" / "night" / f"{T.isoformat()}.json"
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    assert result["ready"] is True
    assert payload["ready"] is True
    assert payload["candidates"] == ["S02"]
    assert payload["held"] == ["S03"]
    assert {"S01", "S02", "S03"} <= set(payload["relevant_symbols"])
    assert {"S01", "S02", "S03"} <= set(payload["history_hashes"])
    assert len(payload["table_hash"]) == 64
    assert payload["pins"] == world.reg["pins"]
    assert "S03" in seen["symbols"]
    assert seen["snapshot_fn"] is not None
    assert (world.data / "forward" / "h2s" / "parity" / f"{T.isoformat()}.json").exists()


def test_night_without_day_bars_saves_no_universe_snapshot(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-20: the first not-ready night saved the write-once snapshot from a stale
    symbol list; the late pipeline then re-synced instruments, and every retry hit
    FileExistsError. The snapshot must wait until day-t bars exist."""
    day_dir = world.data / "kline_daily_enriched" / f"date={T.isoformat()}"
    day_bars = pl.read_parquet(day_dir / "part.parquet")
    for path in day_dir.iterdir():
        path.unlink()
    day_dir.rmdir()
    snapshot = world.data / "forward" / "universe" / f"{T.isoformat()}.parquet"
    snapshot.unlink()

    first = run_night(world.data, T, repo_root=world.repo)

    assert first["ready"] is False
    assert "SPY" in first["reason"]
    assert not snapshot.exists()

    # The late pipeline re-syncs instruments (S03 drops out), then writes day-t bars.
    instruments = world.data / "instruments" / "instruments.parquet"
    pl.read_parquet(instruments).filter(pl.col("symbol") != "S03").write_parquet(instruments)
    day_dir.mkdir(parents=True)
    day_bars.write_parquet(day_dir / "part.parquet")

    monkeypatch.setattr(
        night,
        "extend_points",
        lambda *args, **kwargs: {"extended": [], "replayed": [], "failed": []},
    )
    _patch_point_loading(monkeypatch, night)
    monkeypatch.setattr(night, "candidates_on", lambda entries, elig, t: [])
    monkeypatch.setattr(
        night,
        "forward_table",
        lambda variant, inputs, elig, bars: (_empty_table(), {"kept": 0}),
    )
    monkeypatch.setattr(
        night,
        "VARIANTS",
        {
            "H2-spring": lambda inputs: SimpleNamespace(entries=pl.DataFrame()),
            "H6-H4-3B": lambda inputs: SimpleNamespace(entries=pl.DataFrame()),
        },
    )

    second = run_night(
        world.data,
        T,
        repo_root=world.repo,
        snapshot_fn=lambda frame: {"status": "available"},
    )

    assert second["ready"] is True
    assert pl.read_parquet(snapshot)["symbol"].to_list() == ["S00", "S01", "S02", "SPY"]


def test_night_degrades_a_no_plan_day(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    day = DAYS[40]
    decision_dir = world.data / "forward" / "h2s" / "decision"
    decision_dir.mkdir(parents=True, exist_ok=True)
    (decision_dir / f"{day.isoformat()}.json").write_text(
        json.dumps(
            {
                "date": day.isoformat(),
                "actions": [
                    {
                        "kind": "buy",
                        "symbol": "S01",
                        "shares": 10,
                        "price": 50.0,
                        "reason": "entry",
                        "signal_date": DAYS[39].isoformat(),
                    }
                ],
                "input_hash": "h",
            }
        ),
        encoding="utf-8",
    )
    record(world.data, "h2s", day, "no_plan", "night was not ready")
    table = _table([("S01", day, True, False, 1.0)])
    trade = {
        "symbol": "S01",
        "entry_date": day.isoformat(),
        "exit_date": "2025-06-01",
        "shares": 11,
        "entry_price": 50.0,
        "exit_price": 60.0,
        "exit_reason": "signal",
    }

    monkeypatch.setattr(
        night,
        "extend_points",
        lambda *args, **kwargs: {
            "extended": [],
            "replayed": [],
            "failed": [],
        },
    )
    _patch_point_loading(monkeypatch, night)
    monkeypatch.setattr(night, "candidates_on", lambda entries, elig, t: [])
    monkeypatch.setattr(
        night, "forward_table", lambda variant, inputs, elig, bars: (table, {"kept": 1})
    )
    monkeypatch.setattr(
        night,
        "VARIANTS",
        {
            "H2-spring": lambda inputs: _fake_signals([]),
            "H6-H4-3B": lambda inputs: _fake_signals([]),
        },
    )
    monkeypatch.setattr(
        night, "run_engine", lambda *args, **kwargs: SimpleNamespace(trades=[trade])
    )

    run_night(world.data, T, repo_root=world.repo)

    payload = json.loads(
        (
            world.data / "forward" / "h2s" / "parity" / f"{T.isoformat()}.json"
        ).read_text(encoding="utf-8")
    )
    assert payload["result"]["logic_mismatch"] == []
    assert payload["result"]["degraded_day"]
    assert payload["result"]["degraded_day"][0]["date"] == day.isoformat()
    statuses = statuses_on(world.data, "h2s", day)
    assert "logic_mismatch" not in statuses
    assert "no_plan" in statuses


# ── morning ─────────────────────────────────────────────────────
def test_morning_refuses_on_pin_change(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_night(world.data, "h2s", relevant=["S00"], candidates=["S00"])
    _write_night(world.data, "h6h43b", relevant=["S00"])
    _patch_point_loading(monkeypatch, morning)
    _patch_morning_table(
        monkeypatch, _table([("S00", T, True, False, 1.0)])
    )
    clock, ctx, broker, quotes, factory = _morning_setup(world)

    rel = config.PINNED_FILES[0]
    (world.repo / rel).write_text("# changed pin\n", encoding="utf-8", newline="\n")

    with pytest.raises(registration.PinMismatch):
        _run_morning(
            world,
            clock=clock,
            ctx=ctx,
            broker=broker,
            quotes=quotes,
            executor_factory=factory,
            dry_run=False,
        )
    assert _order_calls(ctx) == []


def test_dry_run_sends_nothing(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_night(world.data, "h2s", relevant=["S00"], candidates=["S00"])
    _write_night(world.data, "h6h43b")
    _patch_point_loading(monkeypatch, morning)
    _patch_morning_table(monkeypatch, _table([("S00", T, True, False, 1.0)]))
    clock, ctx, broker, quotes, factory = _morning_setup(world)

    _run_morning(
        world,
        clock=clock,
        ctx=ctx,
        broker=broker,
        quotes=quotes,
        executor_factory=factory,
        dry_run=True,
    )

    assert _order_calls(ctx) == []
    decision = _decision(world)
    assert decision["date"] == T1.isoformat()
    assert [action["kind"] for action in decision["actions"]] == ["buy"]
    assert decision["opens"]["S00"] == pytest.approx(_opens()["S00"])


def test_morning_eligibility_matches_full_universe(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reshape_s01_amounts(world.data)
    params = UniverseParams(top_n=2, min_bars=1)
    monkeypatch.setattr(tables, "UniverseParams", lambda: params)
    # Night and morning resolve the params through ``tables`` today; patch the
    # module globals too so a future direct import cannot bypass the fixture.
    monkeypatch.setattr(night, "UniverseParams", lambda: params, raising=False)
    monkeypatch.setattr(morning, "UniverseParams", lambda: params, raising=False)

    signals = _fake_signals(
        [
            ("S01", DAYS[20], 1.0),
            ("S01", T, 1.0),
            ("S03", T, 1.0),
        ],
        [("S01", DAYS[30])],
    )
    variants = {
        "H2-spring": lambda inputs: signals,
        "H6-H4-3B": lambda inputs: signals,
    }
    monkeypatch.setattr(tables, "VARIANTS", variants)
    monkeypatch.setattr(night, "VARIANTS", variants)
    monkeypatch.setattr(
        night,
        "extend_points",
        lambda *args, **kwargs: {
            "extended": [],
            "replayed": [],
            "failed": [],
        },
    )
    _patch_point_loading(monkeypatch, night)
    monkeypatch.setattr(
        night, "run_engine", lambda *args, **kwargs: SimpleNamespace(trades=[])
    )

    run_night(world.data, T, repo_root=world.repo)

    elig_path = world.data / "forward" / "night" / f"{T.isoformat()}-elig.parquet"
    assert elig_path.is_file()
    saved = pl.read_parquet(elig_path)
    s01 = saved.filter((pl.col("symbol") == "S01") & (pl.col("date") == T))
    assert s01["eligible"].to_list() == [False]
    assert s01["rank"].to_list() == [4]
    s03 = saved.filter((pl.col("symbol") == "S03") & (pl.col("date") == T))
    assert s03["eligible"].to_list() == [True]

    real_forward_table = tables.forward_table
    captured: dict[str, tuple[pl.DataFrame, pl.DataFrame]] = {}

    def spy_table(variant_id: str, inputs, elig, bars):
        table, log = real_forward_table(variant_id, inputs, elig, bars)
        captured[variant_id] = (elig.clone(), table)
        return table, log

    monkeypatch.setattr(morning, "forward_table", spy_table)
    _patch_point_loading(monkeypatch, morning)
    clock, ctx, broker, quotes, factory = _morning_setup(world)
    _run_morning(
        world,
        clock=clock,
        ctx=ctx,
        broker=broker,
        quotes=quotes,
        executor_factory=factory,
        dry_run=True,
    )

    morning_elig, morning_table = captured["H2-spring"]
    assert morning_elig.filter(
        (pl.col("symbol") == "S01") & (pl.col("date") == T)
    )["eligible"].to_list() == [False]
    assert morning_elig.filter(
        (pl.col("symbol") == "S03") & (pl.col("date") == T)
    )["eligible"].to_list() == [True]
    for added in (T1, PAD):
        rows = morning_elig.filter(pl.col("date") == added)
        assert rows.height > 0
        assert not rows["eligible"].any()

    all_bars = load_forward_bars(world.data, end=T)
    full_elig = tables.forward_eligibility(world.data, all_bars, T)
    assert (
        saved.select("symbol", "date", "eligible", "rank")
        .sort("symbol", "date")
        .equals(
            full_elig.select("symbol", "date", "eligible", "rank").sort(
                "symbol", "date"
            )
        )
    )

    # The full-universe table also needs the open-only t1/pad rows so an entry
    # dated t can pass compose's next-open check.
    extra = morning.open_only_rows(
        all_bars.filter(pl.col("date") == T), _opens(), T1, PAD
    )
    table_bars = pl.concat(
        [
            all_bars,
            extra.select([column for column in all_bars.columns if column in extra.columns]),
        ],
        how="vertical_relaxed",
    )
    added = pl.DataFrame(
        {
            "symbol": [symbol for symbol in ALL_SYMBOLS for _ in (T1, PAD)],
            "date": [day for _ in ALL_SYMBOLS for day in (T1, PAD)],
            "eligible": [False] * (2 * len(ALL_SYMBOLS)),
            "rank": [None] * (2 * len(ALL_SYMBOLS)),
        },
        schema={
            "symbol": pl.String,
            "date": pl.Date,
            "eligible": pl.Boolean,
            "rank": pl.Int64,
        },
    )
    expected_elig = pl.concat(
        [full_elig.select("symbol", "date", "eligible", "rank"), added],
        how="vertical",
    )
    full_inputs = tables.build_inputs(
        table_bars,
        table_bars.filter(pl.col("symbol") == "SPY"),
        pl.DataFrame(),
        pl.DataFrame(),
        pl.DataFrame(),
    )
    expected_table, _log = real_forward_table(
        "H2-spring", full_inputs, expected_elig, table_bars
    )
    expected_t = set(
        expected_table.filter((pl.col("date") == T) & pl.col("entry"))["symbol"]
    )
    morning_t = set(
        morning_table.filter((pl.col("date") == T) & pl.col("entry"))["symbol"]
    )
    assert expected_t == {"S03"}
    assert morning_t == expected_t

    buys = [
        action["symbol"]
        for action in _decision(world, "h2s")["actions"]
        if action["kind"] == "buy"
    ]
    assert "S01" not in buys
    assert "S03" in buys


def test_open_only_template_carries_enriched_columns(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_day = world.data / "kline_daily_enriched" / f"date={T.isoformat()}" / "part.parquet"
    assert "ma20" in pl.read_parquet(source_day).columns
    _write_night(world.data, "h2s", relevant=["S00"], candidates=["S00"])
    _write_night(world.data, "h6h43b")
    _patch_point_loading(monkeypatch, morning)
    _patch_morning_table(monkeypatch, _table([("S00", T, True, False, 1.0)]))
    clock, ctx, broker, quotes, factory = _morning_setup(world)

    _run_morning(
        world,
        clock=clock,
        ctx=ctx,
        broker=broker,
        quotes=quotes,
        executor_factory=factory,
        dry_run=True,
    )

    # The engine store built and the enriched open-only template kept ma20.
    actions = _decision(world)["actions"]
    assert [action["kind"] for action in actions] == ["buy"]


def test_kill_flag_blocks_orders(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_night(world.data, "h2s", relevant=["S00"], candidates=["S00"])
    _write_night(world.data, "h6h43b")
    _patch_point_loading(monkeypatch, morning)
    _patch_morning_table(monkeypatch, _table([("S00", T, True, False, 1.0)]))
    (world.data / "forward" / "STOP").write_text("stop\n", encoding="utf-8")
    clock, ctx, broker, quotes, factory = _morning_setup(world)

    _run_morning(
        world,
        clock=clock,
        ctx=ctx,
        broker=broker,
        quotes=quotes,
        executor_factory=factory,
        dry_run=False,
    )

    assert _order_calls(ctx) == []


def test_no_night_record_only_exits(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_lot(world.data, "h2s", "S01", 40, _price("S01", 20))
    _patch_point_loading(monkeypatch, morning)
    # An entry signal on t would be a buy; without a night record it must drop.
    _patch_morning_table(
        monkeypatch,
        _table(
            [
                ("S01", DAYS[40], True, False, 1.0),
                ("S01", T, False, True, 0.0),
                ("S00", T, True, False, 2.0),
            ]
        ),
    )
    clock, ctx, broker, quotes, factory = _morning_setup(world)
    ctx.positions = {"S01": 40}

    _run_morning(
        world,
        clock=clock,
        ctx=ctx,
        broker=broker,
        quotes=quotes,
        executor_factory=factory,
        dry_run=False,
    )

    calls = _order_calls(ctx)
    assert [call["kwargs"]["trd_side"] for call in calls] == [TrdSide.SELL]
    assert "no_plan" in statuses_on(world.data, "h2s", T1)


def test_sells_before_buys_and_pacing(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_night(
        world.data,
        "h2s",
        relevant=["S00", "S01"],
        held=["S01"],
        candidates=["S00"],
    )
    _write_night(world.data, "h6h43b")
    _seed_lot(world.data, "h2s", "S01", 40, _price("S01", 20))
    _patch_point_loading(monkeypatch, morning)
    _patch_morning_table(
        monkeypatch,
        _table(
            [
                ("S01", DAYS[40], True, False, 1.0),
                ("S01", T, False, True, 0.0),
                ("S00", T, True, False, 2.0),
            ]
        ),
    )
    clock, ctx, broker, quotes, factory = _morning_setup(world)
    ctx.positions = {"S01": 40}

    _run_morning(
        world,
        clock=clock,
        ctx=ctx,
        broker=broker,
        quotes=quotes,
        executor_factory=factory,
        dry_run=False,
    )

    calls = _order_calls(ctx)
    sides = [call["kwargs"]["trd_side"] for call in calls]
    assert sides == [TrdSide.SELL, TrdSide.BUY]
    sell_qty = calls[0]["kwargs"]["qty"]
    assert sell_qty == 40
    assert calls[0]["kwargs"]["code"] == "US.S01"
    assert calls[0]["kwargs"]["remark"] == f"fw-h2s-{T1:%Y%m%d}-S01-S"
    assert calls[1]["kwargs"]["remark"] == f"fw-h2s-{T1:%Y%m%d}-S00-B"
    assert calls[1]["time"] - calls[0]["time"] >= 2.5


def test_decision_record_reproducible(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_night(world.data, "h2s", relevant=["S00"], candidates=["S00"])
    _write_night(world.data, "h6h43b")
    _patch_point_loading(monkeypatch, morning)
    _patch_morning_table(monkeypatch, _table([("S00", T, True, False, 1.0)]))

    first_clock, first_ctx, broker, quotes, factory = _morning_setup(world)
    _run_morning(
        world,
        clock=first_clock,
        ctx=first_ctx,
        broker=broker,
        quotes=quotes,
        executor_factory=factory,
        dry_run=True,
    )
    first = _decision(world)["actions"]

    second_clock, second_ctx, broker, quotes, factory = _morning_setup(world)
    _run_morning(
        world,
        clock=second_clock,
        ctx=second_ctx,
        broker=broker,
        quotes=quotes,
        executor_factory=factory,
        dry_run=True,
    )
    second = _decision(world)["actions"]

    assert first == second
    assert first
    assert _order_calls(first_ctx) == []
    assert _order_calls(second_ctx) == []


def test_missed_open_after_10(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_night(
        world.data,
        "h2s",
        relevant=["S00", "S01"],
        held=["S01"],
        candidates=["S00"],
    )
    _write_night(world.data, "h6h43b")
    _seed_lot(world.data, "h2s", "S01", 40, _price("S01", 20))
    _patch_point_loading(monkeypatch, morning)
    _patch_morning_table(
        monkeypatch,
        _table(
            [
                ("S01", DAYS[40], True, False, 1.0),
                ("S01", T, False, True, 0.0),
                ("S00", T, True, False, 2.0),
            ]
        ),
    )
    clock, ctx, broker, quotes, factory = _morning_setup(
        world, start_time=dt_time(10, 1)
    )

    _run_morning(
        world,
        clock=clock,
        ctx=ctx,
        broker=broker,
        quotes=quotes,
        executor_factory=factory,
        dry_run=False,
    )

    assert _order_calls(ctx) == []
    statuses = statuses_on(world.data, "h2s", T1)
    assert "missed_open" in statuses
    assert "exit_delayed" in statuses


def test_weekly_replay_mismatch_stops_entries(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_night(world.data, "h2s", relevant=["S00"], candidates=["S00"])
    _write_night(world.data, "h6h43b")
    record(world.data, "h2s", T, "replay_mismatch", "S00 differs")
    _patch_point_loading(monkeypatch, morning)
    _patch_morning_table(monkeypatch, _table([("S00", T, True, False, 1.0)]))
    clock, ctx, broker, quotes, factory = _morning_setup(world)

    _run_morning(
        world,
        clock=clock,
        ctx=ctx,
        broker=broker,
        quotes=quotes,
        executor_factory=factory,
        dry_run=False,
    )

    assert _order_calls(ctx) == []
    assert "no_plan" in statuses_on(world.data, "h2s", T1)


def test_engine_entry_prices_latest_held_buy(
    world: SimpleNamespace,
) -> None:
    decision_dir = world.data / "forward" / "h2s" / "decision"
    decision_dir.mkdir(parents=True, exist_ok=True)
    for day, price in ((DAYS[10], 100.0), (DAYS[20], 110.0)):
        (decision_dir / f"{day.isoformat()}.json").write_text(
            json.dumps(
                {
                    "date": day.isoformat(),
                    "actions": [
                        {
                            "kind": "buy",
                            "symbol": "S01",
                            "shares": 10,
                            "price": price,
                            "reason": "entry",
                            "signal_date": "2025-01-01",
                        },
                        {
                            "kind": "buy",
                            "symbol": "S02",
                            "shares": 5,
                            "price": 50.0,
                            "reason": "entry",
                            "signal_date": "2025-01-01",
                        },
                    ],
                    "input_hash": "h",
                }
            ),
            encoding="utf-8",
        )
    _seed_lot(world.data, "h2s", "S01", 10, 100.0)

    assert engine_entry_prices(world.data, "h2s") == {"S01": 110.0}


def test_morning_fallback_eligibility_is_full_universe(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reshape_s01_amounts(world.data)
    params = UniverseParams(top_n=2, min_bars=1)
    monkeypatch.setattr(tables, "UniverseParams", lambda: params)
    # Night and morning resolve the params through ``tables`` today; patch the
    # module globals too so a future direct import cannot bypass the fixture.
    monkeypatch.setattr(night, "UniverseParams", lambda: params, raising=False)
    monkeypatch.setattr(morning, "UniverseParams", lambda: params, raising=False)

    signals = _fake_signals(
        [
            ("S01", DAYS[20], 1.0),
            ("S01", T, 1.0),
            ("S03", T, 1.0),
        ],
        [("S01", DAYS[30])],
    )
    variants = {
        "H2-spring": lambda inputs: signals,
        "H6-H4-3B": lambda inputs: signals,
    }
    monkeypatch.setattr(tables, "VARIANTS", variants)
    monkeypatch.setattr(night, "VARIANTS", variants)
    monkeypatch.setattr(
        night,
        "extend_points",
        lambda *args, **kwargs: {
            "extended": [],
            "replayed": [],
            "failed": [],
        },
    )
    _patch_point_loading(monkeypatch, night)
    monkeypatch.setattr(
        night, "run_engine", lambda *args, **kwargs: SimpleNamespace(trades=[])
    )

    run_night(world.data, T, repo_root=world.repo)

    elig_path = world.data / "forward" / "night" / f"{T.isoformat()}-elig.parquet"
    assert elig_path.is_file()
    saved = pl.read_parquet(elig_path)
    s01 = saved.filter((pl.col("symbol") == "S01") & (pl.col("date") == T))
    assert s01["eligible"].to_list() == [False]
    assert s01["rank"].to_list() == [4]
    s03 = saved.filter((pl.col("symbol") == "S03") & (pl.col("date") == T))
    assert s03["eligible"].to_list() == [True]

    # Night file missing: the morning must recompute eligibility on the FULL
    # forward universe, never on its restricted symbol set.
    elig_path.unlink()

    real_forward_table = tables.forward_table
    captured: dict[str, tuple[pl.DataFrame, pl.DataFrame]] = {}

    def spy_table(variant_id: str, inputs, elig, bars):
        table, log = real_forward_table(variant_id, inputs, elig, bars)
        captured[variant_id] = (elig.clone(), table)
        return table, log

    monkeypatch.setattr(morning, "forward_table", spy_table)
    _patch_point_loading(monkeypatch, morning)
    clock, ctx, broker, quotes, factory = _morning_setup(world)
    _run_morning(
        world,
        clock=clock,
        ctx=ctx,
        broker=broker,
        quotes=quotes,
        executor_factory=factory,
        dry_run=True,
    )

    morning_elig, _morning_table = captured["H2-spring"]
    assert morning_elig.filter(
        (pl.col("symbol") == "S01") & (pl.col("date") == T)
    )["eligible"].to_list() == [False]
    assert morning_elig.filter(
        (pl.col("symbol") == "S03") & (pl.col("date") == T)
    )["eligible"].to_list() == [True]


# ── review fixes (2026-11-19) ───────────────────────────────────


def _live(world, clock, ctx, broker, quotes, factory) -> dict:
    return _run_morning(
        world,
        clock=clock,
        ctx=ctx,
        broker=broker,
        quotes=quotes,
        executor_factory=factory,
        dry_run=False,
    )


def test_lot_the_engine_no_longer_holds_is_sold(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lot left behind by a failed exit, a missed stop or a late morning: the
    # engine already sold it in its own run, so no action will ever come again.
    _write_night(world.data, "h2s", relevant=["S00"], candidates=["S00"])
    _write_night(world.data, "h6h43b")
    _seed_lot(world.data, "h2s", "S02", 25, _price("S02", 20))
    _patch_point_loading(monkeypatch, morning)
    _patch_morning_table(monkeypatch, _table([("S00", T, True, False, 1.0)]))
    clock, ctx, broker, quotes, factory = _morning_setup(world)
    ctx.positions = {"S02": 25}

    _live(world, clock, ctx, broker, quotes, factory)

    sells = [c["kwargs"] for c in _order_calls(ctx) if c["kwargs"]["trd_side"] == TrdSide.SELL]
    assert [(c["code"], c["qty"], c["remark"]) for c in sells] == [
        ("US.S02", 25, f"fw-h2s-{T1:%Y%m%d}-S02-S")
    ]
    assert "catch-up" in statuses_on(world.data, "h2s", T1)["exit_delayed"]
    decision = _decision(world)
    assert [a["symbol"] for a in decision["catch_up"]] == ["S02"]
    assert all(a["symbol"] != "S02" for a in decision["actions"])


def test_logic_mismatch_holds_entries_until_acknowledged(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_night(world.data, "h2s", relevant=["S00"], candidates=["S00"])
    _write_night(world.data, "h6h43b")
    record(world.data, "h2s", DAYS[-5], "logic_mismatch", "S03 shares differ")
    _patch_point_loading(monkeypatch, morning)
    _patch_morning_table(monkeypatch, _table([("S00", T, True, False, 1.0)]))

    clock, ctx, broker, quotes, factory = _morning_setup(world)
    _live(world, clock, ctx, broker, quotes, factory)
    assert _order_calls(ctx) == []
    assert "logic_mismatch" in statuses_on(world.data, "h2s", T1)["no_plan"]

    ack = world.data / "forward" / "h2s" / "ack.txt"
    ack.write_text(DAYS[-5].isoformat() + "\n", encoding="utf-8")
    clock, ctx, broker, quotes, factory = _morning_setup(world)
    _live(world, clock, ctx, broker, quotes, factory)
    assert [c["kwargs"]["code"] for c in _order_calls(ctx)] == ["US.S00"]


def test_open_from_before_todays_session_is_not_used(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_night(world.data, "h2s", relevant=["S00"], candidates=["S00"])
    _write_night(world.data, "h6h43b")
    _patch_point_loading(monkeypatch, morning)
    _patch_morning_table(monkeypatch, _table([("S00", T, True, False, 1.0)]))
    clock, ctx, broker, _quotes, factory = _morning_setup(world)
    yesterday = datetime.combine(T, dt_time(15, 59), tzinfo=ET).timestamp()
    quotes = FakeQuotes(_opens(), times={"S00": yesterday})

    _live(world, clock, ctx, broker, quotes, factory)

    assert _order_calls(ctx) == []
    assert "S00" not in _decision(world)["opens"]
    assert "no_open" in statuses_on(world.data, "h2s", T1)


def test_one_strategy_failing_does_not_stop_the_other(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_night(world.data, "h2s", relevant=["S00"], candidates=["S00"])
    _write_night(world.data, "h6h43b", relevant=["S00"], candidates=["S00"])
    _patch_point_loading(monkeypatch, morning)
    table = _table([("S00", T, True, False, 1.0)])

    def fake_table(variant_id: str, inputs, elig, bars):
        if variant_id == "H2-spring":
            raise RuntimeError("signal table broke")
        return table, {"kept": 1}

    monkeypatch.setattr(morning, "forward_table", fake_table)
    clock, ctx, broker, quotes, factory = _morning_setup(world)

    result = _live(world, clock, ctx, broker, quotes, factory)

    assert "signal table broke" in statuses_on(world.data, "h2s", T1)["no_plan"]
    assert "error" in result["strategies"]["h2s"]
    assert [c["kwargs"]["remark"] for c in _order_calls(ctx)] == [
        f"fw-h6h43b-{T1:%Y%m%d}-S00-B"
    ]


def test_both_strategies_send_before_any_polling(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_night(world.data, "h2s", relevant=["S00"], candidates=["S00"])
    _write_night(world.data, "h6h43b", relevant=["S00"], candidates=["S00"])
    _patch_point_loading(monkeypatch, morning)
    table = _table([("S00", T, True, False, 1.0)])
    monkeypatch.setattr(morning, "forward_table", lambda *args: (table, {"kept": 1}))
    clock, ctx, broker, quotes, factory = _morning_setup(world)

    _live(world, clock, ctx, broker, quotes, factory)

    places = [c["time"] for c in ctx.calls_named("place_order")]
    polls = [c["time"] for c in ctx.calls_named("order_list_query")]
    assert len(places) == 2
    assert polls and max(places) <= min(polls)


def test_orders_left_open_on_an_earlier_day_are_settled(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = f"fw-h2s-{T:%Y%m%d}-S03-B"
    journal = _journal(world.data, "h2s")
    journal.append({"key": key, "state": "submitting", "symbol": "S03", "side": "B", "qty": 10})
    journal.append(
        {"key": key, "state": "submitted", "order_id": "OLD", "symbol": "S03", "side": "B", "qty": 10}
    )
    _write_night(world.data, "h2s")
    _write_night(world.data, "h6h43b")
    _patch_point_loading(monkeypatch, morning)
    _patch_morning_table(monkeypatch, _empty_table())
    clock, ctx, broker, quotes, factory = _morning_setup(world)
    ctx.history = [
        {"order_id": "OLD", "code": "US.S03", "remark": key, "order_status": "FILLED_ALL",
         "qty": 10, "dealt_qty": 10, "dealt_avg_price": 35.0, "trd_side": "BUY"}
    ]

    _live(world, clock, ctx, broker, quotes, factory)

    assert journal.state(key) == "filled"


def test_frozen_symbol_blocks_entry(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_night(world.data, "h2s", relevant=["S00"], candidates=["S00"])
    _write_night(world.data, "h6h43b")
    rec = world.data / "forward" / "h2s" / "reconcile" / f"{T.isoformat()}.json"
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps({"frozen_symbols": ["S00"]}), encoding="utf-8")
    _patch_point_loading(monkeypatch, morning)
    _patch_morning_table(monkeypatch, _table([("S00", T, True, False, 1.0)]))
    clock, ctx, broker, quotes, factory = _morning_setup(world)

    _live(world, clock, ctx, broker, quotes, factory)

    assert _order_calls(ctx) == []
    assert "frozen" in statuses_on(world.data, "h2s", T1)["no_plan"]


def test_saved_book_is_checked_against_the_journal_at_start(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.forward.book import LiveBook, save_book

    _write_night(world.data, "h2s")
    _write_night(world.data, "h6h43b")
    save_book(
        world.data / "forward" / "h2s" / "book.json",
        LiveBook("h2s", 1.0, cash=123.0),
    )
    _patch_point_loading(monkeypatch, morning)
    _patch_morning_table(monkeypatch, _empty_table())
    clock, ctx, broker, quotes, factory = _morning_setup(world)

    _live(world, clock, ctx, broker, quotes, factory)

    assert "reconcile_mismatch" in statuses_on(world.data, "h2s", T1)
