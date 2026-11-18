"""Tests for the write-once forward registration and its pins."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from app.forward import config, pins, registration

_FORWARD_FILES = ("__init__.py", "config.py", "pins.py", "registration.py")


@pytest.fixture
def fake_tree(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway repo root with copies of every pinned file plus a fake freeze."""
    repo = tmp_path / "repo"
    for rel in config.PINNED_FILES:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# fake copy of {rel}\n", encoding="utf-8", newline="\n")
    for rel in config.PINNED_TREES:
        nested = repo / rel / "sub" / "deep.py"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("# fake nested research file\n", encoding="utf-8", newline="\n")
    forward = repo / "backend" / "app" / "forward"
    forward.mkdir(parents=True, exist_ok=True)
    for name in _FORWARD_FILES:
        (forward / name).write_text(f"# fake {name}\n", encoding="utf-8", newline="\n")
    data = tmp_path / "data"
    (data / "research").mkdir(parents=True)
    (data / "research" / "registry.frozen.json").write_text(
        '{"sha256": "fake"}\n', encoding="utf-8"
    )
    return repo, data


def _register(repo: Path, data: Path, **overrides: object) -> Path:
    kwargs: dict[str, object] = {
        "capital_per_strategy": 1_000_000.0,
        "start_date": date(2026, 11, 19),
        "account_id": 12345678,
        "var_sr_trials": 0.42,
        "repo_root": repo,
    }
    kwargs.update(overrides)
    return registration.write_registration(data, **kwargs)


def test_current_pins_stable(fake_tree: tuple[Path, Path]) -> None:
    repo, data = fake_tree

    first = pins.current_pins(repo, data)
    second = pins.current_pins(repo, data)

    assert first == second
    for key in ("chan_version", "wyckoff_version", "bridge_hash", "frozen_registry", "forward_code"):
        assert key in first
    for rel in config.PINNED_FILES:
        assert f"file:{rel}" in first


def test_diff_pins_names_changed_key() -> None:
    assert pins.diff_pins({"a": "1", "b": "2"}, {"a": "1", "b": "3"}) == ["b"]
    assert pins.diff_pins({"a": "1", "b": "2"}, {"a": "1"}) == ["b"]
    assert pins.diff_pins({"a": "1"}, {"a": "1", "extra": "2"}) == ["extra"]


def test_write_once(fake_tree: tuple[Path, Path]) -> None:
    repo, data = fake_tree

    path = _register(repo, data)

    assert path == data / "forward" / "registration.json"
    assert registration.load_registration(data)["account_id"] == 12345678
    with pytest.raises(FileExistsError):
        _register(repo, data)


def test_supersede_moves_old(fake_tree: tuple[Path, Path]) -> None:
    repo, data = fake_tree
    _register(repo, data)
    old_sha = registration.registration_sha256(data)

    _register(repo, data, supersede_reason="fix a rule")

    old_path = data / "forward" / "superseded" / f"{old_sha[:8]}.json"
    assert old_path.exists()
    assert hashlib.sha256(old_path.read_bytes()).hexdigest() == old_sha
    payload = registration.load_registration(data)
    assert payload["supersedes"] == old_sha
    assert payload["supersede_reason"] == "fix a rule"


def test_require_pins_raises_on_change(fake_tree: tuple[Path, Path]) -> None:
    repo, data = fake_tree
    _register(repo, data)
    reg = registration.load_registration(data)
    rel = config.PINNED_FILES[0]

    (repo / rel).write_text("# changed\n", encoding="utf-8", newline="\n")

    with pytest.raises(registration.PinMismatch) as excinfo:
        registration.require_pins(reg, repo, data)
    assert f"file:{rel}" in str(excinfo.value)


def test_evaluation_block_is_research_gate(fake_tree: tuple[Path, Path]) -> None:
    repo, data = fake_tree
    _register(repo, data)

    payload = registration.load_registration(data)
    evaluation = payload["evaluation"]

    assert evaluation["gate"] == [
        "cagr_vs_bh",
        "cagr_vs_rand95",
        "trades_ge_100",
        "avg_r_pos",
        "dsr_ge_0_9",
    ]
    assert evaluation["n_trials"] == 71
    assert evaluation["min_months"] == 12
    assert evaluation["min_closed_trades"] == 100
    assert evaluation["verdict_book"] == "engine"
    assert evaluation["var_sr_trials"] == pytest.approx(0.42)


def test_registration_records_both_leads_and_pins(fake_tree: tuple[Path, Path]) -> None:
    repo, data = fake_tree
    _register(repo, data)

    payload = registration.load_registration(data)

    assert set(payload["strategies"]) == set(config.LEADS)
    for lead, short in config.LEADS.items():
        entry = payload["strategies"][lead]
        assert entry["short"] == short
        assert entry["variant_id"] == lead
        assert entry["variant_config"]["base"] == lead
        assert entry["backtest"]["initial_capital"] == pytest.approx(1_000_000.0)
        assert entry["backtest"]["cost_model"] == "us_per_share_v1"
        assert entry["max_hold_days"] == 20
        assert entry["stop_loss"] == pytest.approx(-0.15)
    assert payload["real_never_used"] is True
    assert payload["start_date"] == "2026-11-19"
    assert payload["pins"] == pins.current_pins(repo, data)
    # The JSON on disk is sorted and pretty-printed.
    text = (data / "forward" / "registration.json").read_text(encoding="utf-8")
    assert text == json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def test_research_tree_pinned_recursively(fake_tree: tuple[Path, Path]) -> None:
    # run_table (evaluate.py), chan_replay.py and features.py decide trades too;
    # a change anywhere under the research package must stop the runner.
    repo, data = fake_tree
    before = pins.current_pins(repo, data)
    nested = repo / config.PINNED_TREES[0] / "sub" / "deep.py"
    nested.write_text("# changed\n", encoding="utf-8", newline="\n")
    after = pins.current_pins(repo, data)
    assert pins.diff_pins(before, after) == [f"tree:{config.PINNED_TREES[0]}"]


def test_decision_path_files_pinned() -> None:
    for rel in (
        "backend/app/backtest/matrix.py",
        "backend/app/strategy/engine.py",
        "backend/app/backtest/engine.py",
        "backend/app/strategy/builtin/research_signal_table.py",
    ):
        assert rel in config.PINNED_FILES
    assert "backend/app/research" in config.PINNED_TREES
