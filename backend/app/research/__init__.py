"""Research-only harness package.

Nothing under ``app/`` outside ``app/research`` may import this package: the
live application must not depend on research code or its extra dependencies.
"""

from __future__ import annotations

from datetime import date

# The stored 2026-11-16 bar was written intraday and is partial; never read it.
RESEARCH_LAST_DATE = date(2026, 11, 13)

# Development window ends here; the year after it is the locked holdout.
DEV_END = date(2025, 11, 13)
HOLDOUT_START = date(2025, 11, 14)
HOLDOUT_END = date(2026, 11, 13)

__all__ = [
    "DEV_END",
    "HOLDOUT_END",
    "HOLDOUT_START",
    "RESEARCH_LAST_DATE",
]