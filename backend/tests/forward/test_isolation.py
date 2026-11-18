"""The live web app must not reach the research harness through app.forward."""

from __future__ import annotations

from pathlib import Path


def test_live_app_never_imports_forward() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    app_root = backend_root / "app"
    allowed = (app_root / "forward", app_root / "research")
    offenders = []
    for path in app_root.rglob("*.py"):
        if any(root in path.parents for root in allowed):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "app.forward" in text or "from app import forward" in text:
            offenders.append(str(path.relative_to(backend_root)))
    assert offenders == [], f"live app modules import app.forward: {offenders}"
