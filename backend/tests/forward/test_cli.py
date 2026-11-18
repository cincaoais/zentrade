"""Forward CLI helpers: registration, status, flatten and the kill flag.

Everything runs on fakes: the trade context is ``FakeTradeContext``, quotes are
a tiny local fake and all records live under ``tmp_path``. No OpenD, no network.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.forward import cli_support, config
from app.forward.broker import SimBroker
from app.forward.config import LEADS
from tests.forward.fake_trade import FakeTradeContext

ET = ZoneInfo("America/New_York")
DAY = date(2026, 11, 18)  # Wednesday
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=ET)


class FixedClock:
    """A clock frozen at ``now``; ``sleep`` advances it instead of blocking."""

    def __init__(self, now: datetime) -> None:
        self._now = now
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 1_000.0

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self._now += timedelta(seconds=float(seconds))


class WeekdayCalendar:
    def is_trading_day(self, d: date | None = None) -> bool:
        return (d or DAY).weekday() < 5


class FakeQuotes:
    def __init__(self, last_price: float = 100.0) -> None:
        self.last_price = last_price

    def snapshot(self, symbols: list[str]) -> dict[str, dict]:
        return {
            symbol: {
                "open_price": self.last_price,
                "last_price": self.last_price,
                "update_time": 0.0,
            }
            for symbol in symbols
        }


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
    for name in ("__init__.py", "config.py", "cli_support.py"):
        (forward / name).write_text(f"# fake {name}\n", encoding="utf-8", newline="\n")
    return repo


def _write_registry(data: Path) -> None:
    research = data / "research"
    research.mkdir(parents=True, exist_ok=True)
    (research / "registry.frozen.json").write_text(
        '{"sha256": "fixture"}\n', encoding="utf-8"
    )
    rows = [
        # Latest dev Sharpe per variant defines the trial variance.
        {"variant_id": "A", "period": "dev", "sharpe_daily": 0.5, "n_obs": 250},
        {"variant_id": "B", "period": "dev", "sharpe_daily": 0.7, "n_obs": 250},
        {"variant_id": "C", "period": "dev", "sharpe_daily": 0.9, "n_obs": 250},
        # A later dev rerun for A replaces its earlier value.
        {"variant_id": "A", "period": "dev", "sharpe_daily": 0.6, "n_obs": 250},
        {
            "variant_id": "H2-spring",
            "period": "holdout",
            "table_hash": "h2-table",
            "code_hash": "code",
            "sharpe_daily": 1.2,
            "n_obs": 252,
            "cagr": 0.24,
            "sharpe": 1.2,
            "max_drawdown": -0.18,
            "trades": 74,
            "win_rate": 0.52,
            "avg_r": 0.11,
        },
    ]
    (research / "registry.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8"
    )


@pytest.fixture
def fake_repo(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    data.mkdir()
    repo = _fake_repo(tmp_path)
    _write_registry(data)
    return data, repo


def _broker(ctx: FakeTradeContext) -> SimBroker:
    return SimBroker(ctx.account_id, ctx_factory=lambda: ctx)


def _ctx(**kwargs) -> FakeTradeContext:
    return FakeTradeContext(account_id=123456, **kwargs)


# ── help ────────────────────────────────────────────────────────
def test_cli_help_lists_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.forward import run as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in (
        "rehearse",
        "register",
        "status",
        "flatten",
        "dry-run",
        "daemon",
        "night",
        "morning",
        "reconcile",
        "parity",
        "stop",
        "report",
    ):
        assert name in out


# ── flatten ─────────────────────────────────────────────────────
def test_flatten_requires_confirm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ctx = _ctx(positions={"AAPL": 100, "MSFT": 5})
    result = cli_support.run_flatten(
        tmp_path,
        confirm=False,
        broker=_broker(ctx),
        clock=FixedClock(NOW),
        calendar=WeekdayCalendar(),
    )

    assert result["confirmed"] is False
    assert result["positions"] == {"AAPL": 100, "MSFT": 5}
    assert ctx.calls_named("place_order") == []
    assert ctx.calls_named("modify_order") == []
    out = capsys.readouterr().out
    assert "AAPL" in out and "100" in out


# ── status / test order ────────────────────────────────────────
def test_status_never_places_orders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _ctx(positions={"AAPL": 10})
    result = cli_support.run_status(
        tmp_path,
        check_account=True,
        broker=_broker(ctx),
        clock=FixedClock(NOW),
        calendar=WeekdayCalendar(),
    )

    assert result["account"]["acc_id"] == 123456
    assert result["cash"] == 1_000_000.0
    assert result["positions"] == {"AAPL": 10}
    assert ctx.calls_named("place_order") == []
    assert ctx.calls_named("modify_order") == []
    out = capsys.readouterr().out
    assert "SIMULATE" in out


def test_test_order_is_cancelled_and_read_by_remark(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _ctx(positions={})
    result = cli_support.run_status(
        tmp_path,
        test_order="AAPL",
        broker=_broker(ctx),
        quotes=FakeQuotes(last_price=100.0),
        clock=FixedClock(NOW),
        calendar=WeekdayCalendar(),
    )

    placed = ctx.calls_named("place_order")
    assert len(placed) == 1
    kwargs = placed[0]["kwargs"]
    remark = f"fw-test-{DAY:%Y%m%d}-AAPL"
    assert kwargs["remark"] == remark
    assert kwargs["qty"] == 1
    assert kwargs["price"] == pytest.approx(80.0)
    assert kwargs["time_in_force"] == "DAY"
    assert kwargs["fill_outside_rth"] is False

    assert result["test_order"]["remark"] == remark
    assert result["test_order"]["order_id"] == "ORD1"
    assert result["test_order"]["cancelled_status"] == "CANCELLED_ALL"
    assert len(ctx.calls_named("modify_order")) == 1
    out = capsys.readouterr().out
    assert remark in out


# ── kill flag ───────────────────────────────────────────────────
def test_stop_creates_flag(tmp_path: Path) -> None:
    path = cli_support.run_stop(tmp_path, reason="test")

    assert path == tmp_path / "forward" / "STOP"
    assert path.is_file()
    audit = tmp_path / "forward" / "audit.jsonl"
    events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert any(event.get("event") == "stop_flag" for event in events)


# ── register ────────────────────────────────────────────────────
def test_register_writes_var_sr_and_reference(fake_repo: tuple[Path, Path]) -> None:
    data, repo = fake_repo

    path = cli_support.run_register(
        data,
        capital=250_000.0,
        start_date=date(2026, 11, 20),
        account_id=123456,
        repo_root=repo,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_var = statistics.variance([0.6, 0.7, 0.9])
    assert payload["evaluation"]["var_sr_trials"] == pytest.approx(expected_var)
    assert payload["evaluation"]["n_trials"] == 71

    h2s = payload["strategies"]["H2-spring"]
    assert h2s["reference"] == {
        "cagr": 0.24,
        "sharpe": 1.2,
        "max_drawdown": -0.18,
        "trades": 74,
        "win_rate": 0.52,
        "avg_r": 0.11,
    }
    assert "reference" not in payload["strategies"]["H6-H4-3B"]
    assert set(payload["strategies"]) == set(LEADS)

def test_list_accounts_reads_only_the_account_list() -> None:
    from moomoo import TrdEnv

    from app.forward import cli_support
    from tests.forward.fake_trade import FakeTradeContext

    ctx = FakeTradeContext(
        account_id=7,
        accounts=[
            {"acc_id": 7, "trd_env": TrdEnv.SIMULATE, "trdmarket_auth": ["US"]},
            {"acc_id": 9, "trd_env": "REAL", "trdmarket_auth": ["US"]},
        ],
    )
    rows = cli_support.list_accounts(ctx_factory=lambda: ctx)

    assert [row["acc_id"] for row in rows] == [7, 9]
    assert [call["name"] for call in ctx.calls if call["name"] != "close"] == ["get_acc_list"]


def test_status_with_explicit_account_id_needs_no_registration(tmp_path) -> None:
    from app.forward import cli_support
    from app.forward.broker import SimBroker
    from tests.forward.fake_trade import FakeTradeContext

    ctx = FakeTradeContext(account_id=7)
    made: list[int] = []

    def factory(account_id: int) -> SimBroker:
        made.append(account_id)
        return SimBroker(account_id, ctx_factory=lambda: ctx)

    result = cli_support.run_status(
        tmp_path, check_account=True, account_id=7, broker_factory=factory
    )

    assert made == [7]
    assert result["cash"] == 1_000_000.0
    assert not ctx.calls_named("place_order")
