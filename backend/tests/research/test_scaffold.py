"""Scaffold tests: vendored Chan engine, version helpers, import isolation."""

from __future__ import annotations

import math
import re
from pathlib import Path

import pandas as pd

from app.research.vendor.dsa_chan.engine import build_level_snapshot
from app.research.versions import chan_engine_version


def _zigzag_bars(n: int = 300) -> pd.DataFrame:
    closes = [100 + 10 * math.sin(i / 7) + i * 0.05 for i in range(n)]
    opens = [100.0, *closes[:-1]]
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": opens,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1e6] * n,
            "amount": [c * 1e6 for c in closes],
        }
    )


def test_engine_snapshot_available_on_synthetic_bars() -> None:
    snap = build_level_snapshot(_zigzag_bars(), "1d", "TEST")
    assert snap["status"] == "available"
    assert "buy_sell_points" in snap


def test_chan_engine_version_stable() -> None:
    first = chan_engine_version()
    second = chan_engine_version()
    assert first == second
    assert re.match(r"^[0-9a-f]{16}-czsc1\.0\.2$", first)


def test_file_hash_ignores_line_endings(tmp_path) -> None:
    from app.research.versions import file_hash

    lf = tmp_path / "lf.py"
    crlf = tmp_path / "crlf.py"
    lf.write_bytes(b"a = 1\nb = 2\n")
    crlf.write_bytes(b"a = 1\r\nb = 2\r\n")
    assert file_hash(lf) == file_hash(crlf)


def test_live_app_never_imports_research() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    app_root = backend_root / "app"
    research_root = app_root / "research"
    # The forward-test runner (app/forward) exists to run the frozen research
    # code and is started from its own CLI, never by the web app; the reverse
    # rule (no live module imports app.forward) is tests/forward/test_isolation.py.
    forward_root = app_root / "forward"
    offenders = []
    for path in app_root.rglob("*.py"):
        if research_root in path.parents or path == research_root:
            continue
        if forward_root in path.parents:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "app.research" in text or "from app import research" in text:
            offenders.append(str(path.relative_to(backend_root)))
    assert offenders == [], f"live app modules import app.research: {offenders}"