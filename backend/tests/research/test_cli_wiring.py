"""CLI wiring for the ranking grid: pins, pool worker and freeze."""

from __future__ import annotations

import json
from datetime import date

from app.research.hypotheses import VARIANT_CONFIGS

# ---------------------------------------------------------------------------
# evaluate CLI
# ---------------------------------------------------------------------------

def test_current_pins_include_variant_configs(tmp_path, monkeypatch) -> None:
    from scripts.research import evaluate as cli

    class _Repo:
        def __init__(self, store) -> None:
            self.store = store

        def get_matrix_data_generation(self, asset_type: str = "stock") -> str:
            return "gen-1"

    monkeypatch.setattr(cli, "KlineRepository", _Repo)
    monkeypatch.setattr(cli, "DataStore", lambda path: path)

    pins = cli._current_pins(tmp_path, ["H1c-2B~rs~p5"])

    assert pins.variant_configs == {
        "H1c-2B~rs~p5": {"base": "H1c-2B", "rank": "rs", "slots": 5}
    }

def test_pool_worker_passes_max_positions(tmp_path, monkeypatch) -> None:
    from scripts.research import evaluate as cli

    calls: list[dict] = []

    def fake_run_table(svc, path, period, *args, **kwargs):
        calls.append({"svc": svc, "path": path, "period": period, **kwargs})
        return {
            "equity_curve": [
                {"date": "2024-01-02", "value": 100.0},
                {"date": "2024-01-03", "value": 101.0},
            ]
        }

    service = object()
    monkeypatch.setattr(cli, "run_table", fake_run_table)
    monkeypatch.setattr(cli, "_WORKER_SERVICE", service)

    path = tmp_path / "table.parquet"
    cli._run_worker((str(path), "dev", date(2024, 1, 2), date(2024, 1, 3), 5))

    assert len(calls) == 1
    assert calls[0]["svc"] is service
    assert calls[0]["path"] == path
    assert calls[0]["max_positions"] == 5

# ---------------------------------------------------------------------------
# freeze CLI
# ---------------------------------------------------------------------------

def test_freeze_pins_variant_configs(tmp_path, monkeypatch) -> None:
    from scripts.research import freeze as freeze_cli

    research_dir = tmp_path / "research"
    research_dir.mkdir()
    rows = [
        {
            "variant_id": variant_id,
            "period": "dev",
            "table_hash": "table",
            "code_hash": "code",
            "sharpe_daily": 0.0,
            "n_obs": 1,
            "config": None,
        }
        for variant_id in VARIANT_CONFIGS
    ]
    (research_dir / "registry.json").write_text(json.dumps(rows), encoding="utf-8")

    for variant_id in VARIANT_CONFIGS:
        variant_dir = research_dir / "variants" / variant_id
        variant_dir.mkdir(parents=True)
        (variant_dir / "table.parquet").write_bytes(b"")

    class _Repo:
        def __init__(self, store) -> None:
            self.store = store

        def get_matrix_data_generation(self, asset_type: str = "stock") -> str:
            return "gen-1"

    monkeypatch.setattr(freeze_cli, "KlineRepository", _Repo)
    monkeypatch.setattr(freeze_cli, "DataStore", lambda path: path)

    assert freeze_cli.main(["--data-dir", str(tmp_path)]) == 0

    frozen = json.loads((research_dir / "registry.frozen.json").read_text(encoding="utf-8"))
    variant_configs = frozen["pins"]["variant_configs"]
    assert len(variant_configs) == 71
    assert variant_configs == {
        variant_id: {
            "base": config.base,
            "rank": config.rank,
            "slots": config.slots,
        }
        for variant_id, config in VARIANT_CONFIGS.items()
    }
