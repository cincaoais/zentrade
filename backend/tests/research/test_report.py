"""Tests for the markdown report writer (rank/slots columns and grid)."""

from __future__ import annotations

from pathlib import Path

from app.research.report import write_report


def _result(
    variant_id: str,
    *,
    config: dict | None = None,
    cagr: float | None = 0.219,
    random_percentile: float | None = 82.0,
) -> dict:
    result = {
        "variant_id": variant_id,
        "main": {
            "trades": 12,
            "win_rate": 0.5,
            "avg_r": 0.1,
            "cagr": cagr,
            "sharpe_ann": 1.0,
            "max_drawdown": -0.1,
            "exposure": 0.5,
            "dsr": 0.9,
        },
        "random_percentile": random_percentile,
        "gate": {"passed": True},
    }
    if config is not None:
        result["config"] = config
    return result


def _render(tmp_path: Path, results: list[dict]) -> str:
    out = tmp_path / "report.md"
    write_report(out, "run", results, {}, {}, [], None, "")
    return out.read_text(encoding="utf-8")


def test_summary_has_rank_and_slots_columns(tmp_path: Path) -> None:
    text = _render(
        tmp_path,
        [
            _result(
                "H1c-2B~rs~p5",
                config={"base": "H1c-2B", "rank": "rs", "slots": 5},
            )
        ],
    )
    header = next(line for line in text.splitlines() if line.startswith("| Variant |"))
    assert "Rank" in header
    assert "Slots" in header
    row = next(line for line in text.splitlines() if line.startswith("| H1c-2B~rs~p5 |"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[1] == "rs"
    assert cells[2] == "5"


def test_ranking_grid_section(tmp_path: Path) -> None:
    results = [
        _result("H1c-2B", config=None),
        _result(
            "H1c-2B~rs",
            config={"base": "H1c-2B", "rank": "rs", "slots": 10},
        ),
        _result(
            "H1c-2B~rs~p5",
            config={"base": "H1c-2B", "rank": "rs", "slots": 5},
            cagr=0.10,
            random_percentile=50.0,
        ),
    ]
    text = _render(tmp_path, results)
    assert "## Ranking grid" in text
    assert "Slots 10" in text
    assert "Slots 5" in text

    slots10 = text.split("Slots 10", 1)[1].split("Slots 5", 1)[0]
    row10 = next(line for line in slots10.splitlines() if line.startswith("| H1c-2B |"))
    assert "21.9% / 82%" in row10
    assert "—" in row10

    slots5 = text.split("Slots 5", 1)[1]
    row5 = next(line for line in slots5.splitlines() if line.startswith("| H1c-2B |"))
    assert "10.0% / 50%" in row5


def test_grid_rank_column_order(tmp_path: Path) -> None:
    text = _render(
        tmp_path,
        [_result("H1c-2B", config=None)],
    )
    grid = text.split("## Ranking grid", 1)[1]
    header = next(line for line in grid.splitlines() if line.startswith("| Base |"))
    assert header == "| Base | hash | rs | retr | risk | relvol | lowvol |"


def test_old_results_without_config_still_render(tmp_path: Path) -> None:
    text = _render(tmp_path, [_result("H1c-2B")])
    summary = text.split("## Summary", 1)[1].split("## ", 1)[0]
    row = next(line for line in summary.splitlines() if line.startswith("| H1c-2B |"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[1] == "hash"
    assert cells[2] == "10"
