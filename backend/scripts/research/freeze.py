"""Freeze the holdout pins.

Usage::

    cd backend
    uv run python scripts/research/freeze.py --data-dir <data_dir>

The CLI refuses to freeze unless every variant has a development registry row
and a signal table; it then writes ``<data_dir>/research/registry.frozen.json``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings
from app.research import RESEARCH_LAST_DATE
from app.research.registry import HoldoutLocked, Pins, Registry, freeze
from app.research.universe import UniverseParams
from app.research.versions import chan_engine_version, file_hash, wyckoff_port_version
from app.storage.repository import DataStore, KlineRepository

BACKEND = Path(__file__).resolve().parents[2]
BRIDGE_PATH = BACKEND / "app" / "strategy" / "builtin" / "research_signal_table.py"


def latest_table_hash(data_dir: Path, variant_id: str) -> str | None:
    """Hash (file stem) of the newest parquet in ``variants/<variant_id>/``."""
    directory = Path(data_dir) / "research" / "variants" / variant_id
    files = list(directory.glob("*.parquet")) if directory.is_dir() else []
    if not files:
        return None
    newest = max(files, key=lambda path: (path.stat().st_mtime_ns, path.name))
    return newest.stem


def _fail(message: str) -> int:
    print(f"freeze: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze research holdout pins")
    parser.add_argument(
        "--data-dir",
        default=str(settings.data_dir),
        help="ZenTrade data directory (default: configured data dir)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="frozen registry path (default: <data_dir>/research/registry.frozen.json)",
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    research_dir = data_dir / "research"
    out = Path(args.out) if args.out else research_dir / "registry.frozen.json"

    if not BRIDGE_PATH.exists():
        return _fail(f"bridge strategy not found at {BRIDGE_PATH}; run Task 9 first")

    try:
        from app.research.hypotheses import VARIANTS
    except ImportError as exc:  # pragma: no cover - depends on Task 8 landing
        return _fail(f"cannot load the 18 variant definitions: {exc}")

    variant_ids = list(VARIANTS)
    registry = Registry(research_dir / "registry.json")

    dev_variants = {
        row["variant_id"] for row in registry.rows() if row.get("period") == "dev"
    }
    missing_dev = [variant_id for variant_id in variant_ids if variant_id not in dev_variants]
    if missing_dev:
        return _fail(
            "refusing to freeze: no dev registry row for "
            + ", ".join(missing_dev)
        )

    table_hashes: dict[str, str] = {}
    for variant_id in variant_ids:
        table_hash = latest_table_hash(data_dir, variant_id)
        if table_hash is None:
            return _fail(
                f"refusing to freeze: no table for variant {variant_id} "
                f"under {research_dir / 'variants' / variant_id}"
            )
        table_hashes[variant_id] = table_hash

    repo = KlineRepository(DataStore(data_dir))
    pins = Pins(
        data_generation=repo.get_matrix_data_generation("stock"),
        last_date=RESEARCH_LAST_DATE.isoformat(),
        universe_hash=UniverseParams().hash(),
        chan_version=chan_engine_version(),
        wyckoff_version=wyckoff_port_version(),
        table_hashes=table_hashes,
        bridge_hash=file_hash(BRIDGE_PATH),
    )

    try:
        payload = freeze(registry, pins, variant_ids, out)
    except HoldoutLocked as exc:
        return _fail(exc.reason)
    print(f"froze {len(variant_ids)} variants at {out} (n_trials={payload['n_trials']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())