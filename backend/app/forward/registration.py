"""Write-once forward registration.

``<data>/forward/registration.json`` is written once before day 1 and is
read-only afterwards. Superseding it (a real rule change) moves the old record
to ``forward/superseded/<sha8>.json`` and starts a new clock; records are never
merged.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import date
from pathlib import Path

from app.forward import config
from app.forward import pins as pins_module
from app.research import hypotheses

# The research gate, unchanged (spec section 9).
_EVALUATION: dict = {
    "verdict_book": "engine",
    "min_months": 12,
    "min_closed_trades": 100,
    "gate": [
        "cagr_vs_bh",
        "cagr_vs_rand95",
        "trades_ge_100",
        "avg_r_pos",
        "dsr_ge_0_9",
    ],
    "n_trials": 71,
}

# The research backtest config (Global Constraints); initial capital is added
# per strategy at write time.
_BACKTEST: dict = {
    "max_positions": 10,
    "position_sizing": "equal",
    "entry_fill": "open_t+1",
    "exit_fill": "open_t+1",
    "slippage_bps": 5,
    "cost_model": "us_per_share_v1",
    "mode": "position",
    "strategy": "research_signal_table",
}


class PinMismatch(RuntimeError):  # noqa: N818 - name fixed by the plan
    """A pinned file or code hash changed since registration."""


def registration_path(data_dir: Path) -> Path:
    return config.forward_dir(data_dir) / "registration.json"


def load_registration(data_dir: Path) -> dict:
    return json.loads(registration_path(data_dir).read_text(encoding="utf-8"))


def registration_sha256(data_dir: Path) -> str:
    return hashlib.sha256(registration_path(data_dir).read_bytes()).hexdigest()


def _atomic_write(path: Path, payload: dict) -> None:
    """Write JSON atomically, leaving no temporary file behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _strategy_entries(capital_per_strategy: float) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for lead, short in config.LEADS.items():
        entries[lead] = {
            "variant_id": lead,
            "short": short,
            "variant_config": asdict(hypotheses.variant_config(lead)),
            "backtest": {**_BACKTEST, "initial_capital": capital_per_strategy},
            "max_hold_days": 20,
            "stop_loss": -0.15,
        }
    return entries


def write_registration(
    data_dir: Path,
    *,
    capital_per_strategy: float,
    start_date: date,
    account_id: int,
    var_sr_trials: float,
    repo_root: Path,
    supersede_reason: str | None = None,
) -> Path:
    """Freeze the registration, refusing to overwrite without a stated reason."""
    path = registration_path(data_dir)
    extra: dict = {}
    if path.exists():
        if not supersede_reason:
            raise FileExistsError(
                f"registration already exists at {path}; "
                "pass supersede_reason to replace it"
            )
        old_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        superseded = config.forward_dir(data_dir) / "superseded" / f"{old_sha[:8]}.json"
        superseded.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, superseded)
        extra = {"supersedes": old_sha, "supersede_reason": supersede_reason}

    capital = float(capital_per_strategy)
    payload = {
        "account_id": int(account_id),
        "capital_per_strategy": capital,
        "evaluation": {**_EVALUATION, "var_sr_trials": float(var_sr_trials)},
        "pins": pins_module.current_pins(repo_root, data_dir),
        "real_never_used": True,
        "start_date": start_date.isoformat(),
        "strategies": _strategy_entries(capital),
        **extra,
    }
    _atomic_write(path, payload)
    return path


def require_pins(reg: dict, repo_root: Path, data_dir: Path) -> None:
    """Raise :class:`PinMismatch` naming every pin that no longer matches."""
    expected = dict(reg.get("pins") or {})
    actual = pins_module.current_pins(repo_root, data_dir)
    changed = pins_module.diff_pins(expected, actual)
    if changed:
        raise PinMismatch("forward pins changed: " + ", ".join(changed))


__all__ = [
    "PinMismatch",
    "load_registration",
    "registration_path",
    "registration_sha256",
    "require_pins",
    "write_registration",
]