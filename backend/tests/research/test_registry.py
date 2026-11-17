"""Run registry, freeze and holdout lock."""

from __future__ import annotations

import json

import pytest

from app.research.registry import (
    HoldoutLocked,
    Pins,
    Registry,
    check_holdout_allowed,
    freeze,
)


def _pins(**overrides: object) -> Pins:
    values: dict[str, object] = {
        "data_generation": "gen-1",
        "last_date": "2026-11-13",
        "universe_hash": "univ-1",
        "chan_version": "chan-1",
        "wyckoff_version": "wyk-1",
        "table_hashes": {"A": "tA", "B": "tB"},
        "bridge_hash": "bridge-1",
        "variant_configs": {},
    }
    values.update(overrides)
    return Pins(**values)  # type: ignore[arg-type]


def _record_dev(registry: Registry, variant_id: str, sharpe: float = 0.5) -> None:
    registry.record(
        variant_id=variant_id,
        period="dev",
        table_hash=f"table-{variant_id}",
        code_hash="code-1",
        sharpe_daily=sharpe,
        n_obs=500,
    )


def _frozen(tmp_path, variants: list[str], pins: Pins | None = None):
    registry = Registry(tmp_path / "registry.json")
    pins = pins or _pins()
    out = tmp_path / "registry.frozen.json"
    payload = freeze(registry, pins, variants, out)
    return registry, pins, out, payload


def test_refreeze_refused(tmp_path):
    registry, pins, out, _payload = _frozen(tmp_path, ["A"])
    before = out.read_bytes()
    with pytest.raises(HoldoutLocked, match="re-freeze"):
        freeze(registry, pins, ["A", "B"], out)
    assert out.read_bytes() == before


def test_holdout_refused_without_freeze(tmp_path):
    with pytest.raises(HoldoutLocked):
        check_holdout_allowed(tmp_path / "missing.json", _pins(), "A")


def test_holdout_refused_on_tampered_file(tmp_path):
    _, pins, out, _ = _frozen(tmp_path, ["A"])
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["n_trials"] = 99
    out.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(HoldoutLocked) as exc:
        check_holdout_allowed(out, pins, "A")
    assert "sha256" in str(exc.value)


def test_holdout_refused_on_pin_change(tmp_path):
    _, _, out, _ = _frozen(tmp_path, ["A"])

    with pytest.raises(HoldoutLocked) as exc:
        check_holdout_allowed(out, _pins(chan_version="chan-2"), "A")
    assert "chan_version" in str(exc.value)


def test_holdout_refused_for_unfrozen_variant(tmp_path):
    _, pins, out, _ = _frozen(tmp_path, ["A"])

    with pytest.raises(HoldoutLocked) as exc:
        check_holdout_allowed(out, pins, "B")
    assert "B" in str(exc.value)


def test_n_trials_counts_dev_variants_and_holdout_reruns(tmp_path):
    registry = Registry(tmp_path / "registry.json")
    for variant_id in ("A", "B", "C"):
        _record_dev(registry, variant_id)
    for _ in range(2):
        registry.record(
            variant_id="A",
            period="holdout",
            table_hash="table-A-holdout",
            code_hash="code-1",
            sharpe_daily=0.1,
            n_obs=250,
        )

    assert registry.n_trials() == 4


def test_record_stores_config(tmp_path):
    registry = Registry(tmp_path / "registry.json")
    config = {"base": "A", "rank": "rs", "slots": 5}

    registry.record(
        variant_id="A",
        period="dev",
        table_hash="table-A",
        code_hash="code-1",
        sharpe_daily=0.5,
        n_obs=500,
        config=config,
    )
    registry.record(
        variant_id="B",
        period="dev",
        table_hash="table-B",
        code_hash="code-1",
        sharpe_daily=0.5,
        n_obs=500,
    )

    rows = registry.rows()
    assert rows[0]["config"] == config
    assert rows[1]["config"] is None


def test_pin_variant_configs_checked(tmp_path):
    frozen_pins = _pins(
        variant_configs={"A": {"base": "A", "rank": "hash", "slots": 10}}
    )
    _, _, out, _ = _frozen(tmp_path, ["A"], frozen_pins)

    with pytest.raises(HoldoutLocked) as exc:
        check_holdout_allowed(
            out,
            _pins(variant_configs={"A": {"base": "A", "rank": "hash", "slots": 5}}),
            "A",
        )
    assert "variant_configs" in str(exc.value)


def test_registry_write_atomic(tmp_path):
    path = tmp_path / "registry.json"
    registry = Registry(path)
    _record_dev(registry, "A")

    assert not list(tmp_path.glob("*.tmp"))
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(rows, list)
    assert registry.rows() == rows
    assert rows[0]["variant_id"] == "A"
    assert "run_at" in rows[0]


def test_freeze_payload_shape_and_allowed(tmp_path):
    registry = Registry(tmp_path / "registry.json")
    _record_dev(registry, "A")
    pins = _pins()
    out = tmp_path / "registry.frozen.json"

    payload = freeze(registry, pins, ["A"], out)

    assert set(payload) == {"variants", "n_trials", "pins", "frozen_at", "sha256"}
    assert payload["variants"] == ["A"]
    assert payload["n_trials"] == 1
    assert json.loads(out.read_text(encoding="utf-8")) == payload
    check_holdout_allowed(out, pins, "A")