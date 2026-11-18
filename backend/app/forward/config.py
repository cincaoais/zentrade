"""Forward-test paths, leads and the research files whose hashes are pinned."""

from __future__ import annotations

from pathlib import Path

# Lead variant id -> short id used in order keys and record names.
LEADS: dict[str, str] = {"H2-spring": "h2s", "H6-H4-3B": "h6h43b"}

# Repo-relative files that the forward test must never see change after
# registration. The research run itself is pinned the same way by the freeze.
PINNED_FILES: tuple[str, ...] = (
    "backend/app/backtest/engine.py",
    "backend/app/backtest/strategy.py",
    "backend/app/backtest/matrix.py",
    "backend/app/strategy/engine.py",
    "backend/app/strategy/builtin/research_signal_table.py",
    "backend/app/research/compose.py",
    "backend/app/research/hypotheses.py",
    "backend/app/research/universe.py",
    "backend/app/research/ranking.py",
    "backend/app/research/bars.py",
)

# Directories hashed recursively (every ``*.py``, ``__pycache__`` skipped): the
# whole research package decides trades (run_table, chan_replay, features, ...).
PINNED_TREES: tuple[str, ...] = ("backend/app/research",)


def forward_dir(data_dir: Path) -> Path:
    """Directory holding every forward record, journal and registration."""
    return Path(data_dir) / "forward"


__all__ = ["LEADS", "PINNED_FILES", "PINNED_TREES", "forward_dir"]