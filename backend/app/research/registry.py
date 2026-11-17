"""Run registry, freeze pins and the holdout lock.

The registry is append-only research metadata stored as data, not code:
``<data_dir>/research/registry.json``. A freeze snapshots the trial count and
the pins (data generation, universe/engine versions) that a holdout run must
still match; ``registry.frozen.json`` carries a sha256 over its own content so
that later edits are detected.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

_PERIODS = ("dev", "holdout")
_PIN_FIELDS = (
    "data_generation",
    "last_date",
    "universe_hash",
    "chan_version",
    "wyckoff_version",
    "table_hashes",
    "bridge_hash",
)


class HoldoutLocked(RuntimeError):  # noqa: N818 - name fixed by the plan
    """Raised when a holdout run is not allowed by the frozen registry."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _atomic_write(path: Path, payload: object) -> None:
    """Write JSON atomically, leaving no temporary file behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Pins:
    """Inputs a frozen holdout must still match exactly."""

    data_generation: str
    last_date: str
    universe_hash: str
    chan_version: str
    wyckoff_version: str
    table_hashes: dict[str, str]
    bridge_hash: str


class Registry:
    """Append-only run log held in ``registry.json``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []

    def record(
        self,
        variant_id: str,
        period: Literal["dev", "holdout"],
        table_hash: str,
        code_hash: str,
        sharpe_daily: float | None,
        n_obs: int,
    ) -> None:
        if period not in _PERIODS:
            raise ValueError(f"unknown period {period!r}")
        rows = self.rows()
        rows.append(
            {
                "variant_id": variant_id,
                "period": period,
                "table_hash": table_hash,
                "code_hash": code_hash,
                "sharpe_daily": sharpe_daily,
                "n_obs": n_obs,
                "run_at": _now_iso(),
            }
        )
        _atomic_write(self.path, rows)

    def n_trials(self) -> int:
        rows = self.rows()
        dev_variants = {
            row["variant_id"] for row in rows if row.get("period") == "dev"
        }
        holdout_counts: dict[str, int] = {}
        for row in rows:
            if row.get("period") != "holdout":
                continue
            variant_id = row["variant_id"]
            holdout_counts[variant_id] = holdout_counts.get(variant_id, 0) + 1
        reruns = sum(max(0, count - 1) for count in holdout_counts.values())
        return len(dev_variants) + reruns

    def var_sr_trials(self) -> float:
        latest: dict[str, float] = {}
        for row in self.rows():
            if row.get("period") != "dev":
                continue
            value = row.get("sharpe_daily")
            if value is not None:
                latest[row["variant_id"]] = float(value)
        values = list(latest.values())
        if len(values) < 2:
            return 0.0
        import statistics

        return float(statistics.variance(values))


def freeze(
    registry: Registry,
    pins: Pins,
    variant_ids: list[str],
    out: Path,
) -> dict:
    """Write ``registry.frozen.json`` and return its payload.

    Freezing is once only: re-freezing after a holdout run would let new variants
    in and reset the trial count, which is the leak the lock exists to stop.
    To start over, the user deletes the frozen file by hand (a visible act).
    """
    if Path(out).exists():
        raise HoldoutLocked(f"already frozen at {out}; refusing to re-freeze")
    body = {
        "variants": list(variant_ids),
        "n_trials": registry.n_trials(),
        "pins": asdict(pins),
        "frozen_at": _now_iso(),
    }
    payload = dict(body)
    payload["sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    _atomic_write(Path(out), payload)
    return payload


def _frozen_sha256(payload: dict) -> str:
    body = {key: payload[key] for key in ("variants", "n_trials", "pins", "frozen_at")}
    return hashlib.sha256(_canonical(body)).hexdigest()


def check_holdout_allowed(
    frozen_path: Path,
    current: Pins,
    variant_id: str,
) -> None:
    """Raise :class:`HoldoutLocked` unless the holdout run is allowed."""
    frozen_path = Path(frozen_path)
    if not frozen_path.exists():
        raise HoldoutLocked(f"holdout locked: no freeze file at {frozen_path}")
    try:
        payload = json.loads(frozen_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HoldoutLocked(f"holdout locked: unreadable freeze file ({exc})") from exc

    expected = payload.get("sha256")
    if expected != _frozen_sha256(payload):
        raise HoldoutLocked("holdout locked: freeze file sha256 does not match")

    frozen_pins = payload.get("pins") or {}
    current_pins = asdict(current)
    for field in _PIN_FIELDS:
        if frozen_pins.get(field) != current_pins.get(field):
            raise HoldoutLocked(f"holdout locked: pin {field} differs from freeze")

    if variant_id not in (payload.get("variants") or []):
        raise HoldoutLocked(
            f"holdout locked: variant {variant_id} is not in the frozen variants"
        )


__all__ = [
    "HoldoutLocked",
    "Pins",
    "Registry",
    "check_holdout_allowed",
    "freeze",
]