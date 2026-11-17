"""Markdown report writer for a research evaluation run.

The report is a plain data renderer: it never recomputes metrics, it only lays
out the numbers produced by :mod:`app.research.evaluate` together with the
baselines, integrity checks, registry rows and the engine deviations list.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

CAVEATS = (
    "Survivorship: moomoo lists only stocks trading today; every baseline uses "
    "the same survivor universe.",
    "Prices are forward-adjusted with today's factors.",
    "Universe classification (ETF/unit/preferred) is today's.",
)


def _number(value: Any, digits: int = 2) -> str:
    """Format a finite number, or an em dash when it is missing."""
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "—"
    return f"{number:.{digits}f}"


def _percent(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "—"
    return f"{number * 100:.{digits}f}%"


def _percent_points(value: Any, digits: int = 0) -> str:
    """Format a value already expressed in percent points (0-100)."""
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "—"
    return f"{number:.{digits}f}%"


def _gate_text(gate: dict | None) -> str:
    if not gate:
        return "n/a (dev)"
    text = "PASS" if gate.get("passed") else "FAIL"
    if gate.get("fragile_to_costs"):
        text += " (fragile)"
    return text


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = [_row(header), _row(["---"] * len(header))]
    lines.extend(_row(row) for row in rows)
    return lines


def _summary(results: list[dict]) -> list[str]:
    lines = [
        "## Summary",
        "",
        "Main run at 5 bps slippage. Random percentile is the share of random-entry "
        "seeds the variant beat; DSR is deflated for the trial count.",
        "",
    ]
    rows: list[list[str]] = []
    for result in results:
        main = result.get("main") or {}
        rows.append(
            [
                str(result.get("variant_id", "")),
                str(main.get("trades", 0)),
                _percent(main.get("win_rate")),
                _number(main.get("avg_r"), 3),
                _percent(main.get("cagr")),
                _number(main.get("sharpe_ann")),
                _percent(main.get("max_drawdown")),
                _percent(main.get("exposure")),
                _percent_points(result.get("random_percentile"), 0),
                _number(main.get("dsr"), 3),
                _gate_text(result.get("gate")),
            ]
        )
    lines.extend(
        _table(
            [
                "Variant",
                "Trades",
                "Win rate",
                "Avg R",
                "CAGR",
                "Sharpe",
                "MDD",
                "Exposure",
                "Random pct",
                "DSR",
                "Gate",
            ],
            rows,
        )
    )
    notes = [
        f"- {result['variant_id']}: random baseline — {result['random_note']}"
        for result in results
        if result.get("random_note")
    ]
    if notes:
        lines.extend(["", *notes])
    return lines


def _baselines(baselines: dict) -> list[str]:
    rows = []
    for key, label in (("b0_bh", "B0-bh (equal-weight eligible buy & hold)"),
                       ("b0_spy", "B0-spy (SPY reference)")):
        stats = baselines.get(key) or {}
        rows.append(
            [
                label,
                _percent(stats.get("cagr")),
                _number(stats.get("sharpe_ann")),
                _percent(stats.get("max_drawdown")),
            ]
        )
    return [
        "## Baselines",
        "",
        "Both baselines trade the same store and costs as the variants.",
        "",
        *_table(["Baseline", "CAGR", "Sharpe", "MDD"], rows),
    ]


def _cost_stress(results: list[dict]) -> list[str]:
    lines = [
        "## Cost stress",
        "",
        "The stress run repeats the main run at 15 bps slippage. The delta is "
        "stress CAGR minus main CAGR.",
        "",
    ]
    rows: list[list[str]] = []
    for result in results:
        main = result.get("main") or {}
        stress = result.get("stress") or {}
        delta = None
        if main.get("cagr") is not None and stress.get("cagr") is not None:
            delta = float(stress["cagr"]) - float(main["cagr"])
        rows.append(
            [
                str(result.get("variant_id", "")),
                _percent(main.get("cagr")),
                _percent(stress.get("cagr")),
                _percent(delta),
                _number(stress.get("sharpe_ann")),
                _percent(stress.get("max_drawdown")),
                _gate_text(result.get("gate")),
            ]
        )
    lines.extend(
        _table(
            [
                "Variant",
                "Main CAGR",
                "Stress CAGR",
                "Δ CAGR",
                "Stress Sharpe",
                "Stress MDD",
                "Gate",
            ],
            rows,
        )
    )
    return lines


def _dev_folds(results: list[dict]) -> list[str]:
    lines = ["## Dev folds", ""]
    for result in results:
        folds = result.get("folds") or []
        lines.append(f"### {result.get('variant_id', '')}")
        lines.append("")
        if not folds:
            lines.extend(["No development folds.", ""])
            continue
        rows = [
            [
                f"{fold.get('start', '')} → {fold.get('end', '')}",
                str(fold.get("trades", 0)),
                _number(fold.get("avg_r"), 3),
                _percent(fold.get("cagr")),
                _number(fold.get("sharpe_ann")),
                _percent(fold.get("max_drawdown")),
            ]
            for fold in folds
        ]
        lines.extend(
            _table(
                ["Window", "Trades", "Avg R", "CAGR", "Sharpe", "MDD"],
                rows,
            )
        )
        lines.append("")
    return lines


def _integrity(integrity: dict) -> list[str]:
    lines = ["## Integrity", ""]
    chan = integrity.get("chan_truncation") or {}
    wyk = integrity.get("wyckoff_truncation") or {}
    lines.extend(
        _table(
            ["Check", "Replays", "Mismatches"],
            [
                [
                    "Chan truncation",
                    str(chan.get("checked", 0)),
                    str(len(chan.get("mismatches") or [])),
                ],
                [
                    "Wyckoff truncation",
                    str(wyk.get("checked", 0)),
                    str(len(wyk.get("mismatches") or [])),
                ],
            ],
        )
    )
    lines.append("")

    mismatch_rows: list[str] = []
    for label, check in (("Chan", chan), ("Wyckoff", wyk)):
        for mismatch in check.get("mismatches") or []:
            mismatch_rows.append(
                f"- {label}: {mismatch.get('symbol', '?')} — {mismatch.get('reason', 'mismatch')}"
            )
    if mismatch_rows:
        lines.extend(["Truncation mismatches:", "", *mismatch_rows, ""])

    phantom = integrity.get("phantom_rate") or []
    lines.extend(["Phantom rate per point type:", ""])
    if phantom:
        lines.extend(
            _table(
                ["Type", "First seen", "Phantoms", "Rate"],
                [
                    [
                        str(row.get("type", "")),
                        str(row.get("first_seen", 0)),
                        str(row.get("phantoms", 0)),
                        _percent(row.get("rate")),
                    ]
                    for row in phantom
                ],
            )
        )
    else:
        lines.append("No Chan points stored.")
    lines.append("")

    determinism = integrity.get("determinism") or {}
    different = determinism.get("different") or []
    lines.extend(
        [
            "Determinism (byte-identical replays into a fresh directory): "
            f"{determinism.get('identical', 0)} identical, {len(different)} different.",
        ]
    )
    if different:
        lines.extend([""] + [f"- different: {symbol}" for symbol in different])
    return lines


def _trials(results: list[dict], registry_rows: list[dict], frozen: dict | None) -> list[str]:
    dev_variants = {
        row.get("variant_id") for row in registry_rows if row.get("period") == "dev"
    }
    holdout_counts: dict[str, int] = {}
    for row in registry_rows:
        if row.get("period") != "holdout":
            continue
        variant_id = row.get("variant_id")
        holdout_counts[variant_id] = holdout_counts.get(variant_id, 0) + 1
    reruns = sum(max(0, count - 1) for count in holdout_counts.values())
    n_trials = results[0].get("n_trials") if results else None
    if n_trials is None:
        n_trials = len(dev_variants) + reruns

    lines = [
        "## Trials",
        "",
        f"- Trial count N: {n_trials}",
        f"- Holdout reruns: {reruns}",
        f"- Registry rows: {len(registry_rows)}",
    ]
    if frozen:
        lines.append(
            f"- Frozen at {frozen.get('frozen_at', '?')} over "
            f"{len(frozen.get('variants') or [])} variants "
            f"(n_trials={frozen.get('n_trials', '?')}, "
            f"sha256={str(frozen.get('sha256', '?'))[:16]}…)"
        )
    else:
        lines.append("- Not frozen (development run only).")

    if registry_rows:
        lines.extend([""])
        lines.extend(
            _table(
                ["Variant", "Period", "Table hash", "Sharpe daily", "Observations", "Run at"],
                [
                    [
                        str(row.get("variant_id", "")),
                        str(row.get("period", "")),
                        str(row.get("table_hash", "")),
                        _number(row.get("sharpe_daily"), 4),
                        str(row.get("n_obs", 0)),
                        str(row.get("run_at", "")),
                    ]
                    for row in registry_rows
                ],
            )
        )
    return lines


def _caveats() -> list[str]:
    return ["## Caveats", "", *[f"- {caveat}" for caveat in CAVEATS]]


def write_report(
    out: Path,
    run_name: str,
    results: list[dict],
    baselines: dict,
    integrity: dict,
    registry_rows: list[dict],
    frozen: dict | None,
    deviations_md: str,
) -> Path:
    """Write the full markdown report to ``out`` and return the path."""
    out = Path(out)
    lines = [
        f"# Research report: {run_name}",
        "",
        "Generated by `scripts/research/evaluate.py`. Every variant trades through "
        "ZenTrade's ordinary backtest service; baselines share the same store, "
        "costs and execution.",
        "",
        *_summary(results),
        "",
        *_baselines(baselines),
        "",
        *_cost_stress(results),
        "",
        *_dev_folds(results),
        *_integrity(integrity),
        "",
        *_trials(results, registry_rows, frozen),
        "",
        *_caveats(),
        "",
        "## Deviations",
        "",
        deviations_md.rstrip(),
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


__all__ = ["CAVEATS", "write_report"]