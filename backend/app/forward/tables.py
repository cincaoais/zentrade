"""Forward decision tables built by the frozen research variant code.

The night and morning steps compose the same tables the research run used:
``app.research.hypotheses.VARIANTS`` produce raw signals and
``app.research.compose.compose_table`` applies eligibility, the next-open gap
check, the active invalidation and the exit flags. This module only loads the
forward inputs and forwards the calls, so a forward table is byte-identical to
the research table for the same bars and points.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.forward.data import forward_tradable
from app.research.compose import compose_table, with_atr14
from app.research.hypotheses import VARIANTS, Inputs
from app.research.ranking import rank_features
from app.research.universe import UniverseParams, eligibility


def build_inputs(
    bars: pl.DataFrame,
    spy: pl.DataFrame,
    chan: pl.DataFrame,
    wyk_events: pl.DataFrame,
    wyk_ranges: pl.DataFrame,
) -> Inputs:
    """Build the research ``Inputs`` the same way ``scripts/research/compose.py`` does.

    ATR is computed once over the full bar store and reused by every variant;
    ranking features are computed once from those ATR-bearing bars and SPY.
    """
    atr_bars = with_atr14(bars)
    return Inputs(
        chan_points=chan,
        wyk_events=wyk_events,
        wyk_ranges=wyk_ranges,
        bars=atr_bars,
        spy=spy,
        features=rank_features(atr_bars, spy),
    )


def forward_table(
    variant_id: str, inputs: Inputs, elig: pl.DataFrame, bars: pl.DataFrame
) -> tuple[pl.DataFrame, dict]:
    """Compose one frozen variant's table for the forward window."""
    return compose_table(VARIANTS[variant_id](inputs), bars, elig)


def forward_eligibility(
    data_dir: Path, bars: pl.DataFrame, d: date
) -> pl.DataFrame:
    """Point-in-time eligibility over the forward universe as of ``d``.

    ``forward_tradable`` is the union of the forward universe snapshots on or
    before ``d``, not today's instrument list, so a later delisting does not
    rewrite history.
    """
    return eligibility(bars, forward_tradable(Path(data_dir), d), UniverseParams())


def kept_entry_symbols(table: pl.DataFrame, start: date) -> set[str]:
    """Symbols with a kept ``entry`` on or after ``start``."""
    if table.is_empty():
        return set()
    rows = table.filter(pl.col("entry") & (pl.col("date") >= start))
    return set(rows["symbol"].to_list())


def candidates_on(
    signals_entries: pl.DataFrame, elig: pl.DataFrame, t: date
) -> list[str]:
    """Eligible day-``t`` entry symbols, before the next-open gap filter.

    ``signals_entries`` is a variant's raw entry frame (decision dates), not a
    composed table. Gapped candidates are removed later by ``compose_table``.
    """
    if signals_entries.is_empty() or elig.is_empty():
        return []
    signals = (
        signals_entries.filter(pl.col("date") == t)
        .select("symbol")
        .unique()
    )
    eligible = (
        elig.filter((pl.col("date") == t) & pl.col("eligible"))
        .select("symbol")
        .unique()
    )
    return sorted(signals.join(eligible, on="symbol", how="semi")["symbol"].to_list())


__all__ = [
    "build_inputs",
    "candidates_on",
    "forward_eligibility",
    "forward_table",
    "kept_entry_symbols",
]