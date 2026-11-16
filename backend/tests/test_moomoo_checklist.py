# ruff: noqa: RUF001  # 面向用户的中文文案/标签, 全角标点是有意的
"""Task 8: setup checklist moomoo item and Alpaca downgrade.

No OpenD is contacted: ``MoomooProvider.opend_status`` is monkeypatched.
"""
from __future__ import annotations

import pytest

ENV_NAMES = (
    "ZENTRADE_MOOMOO_OPEND",
    "ZENTRADE_ALPACA_KEY",
    "ALPACA_API_KEY_ID",
    "ALPACA_API_SECRET_KEY",
    "FINNHUB_API_KEY",
    "AI_API_KEY",
    "ZENTRADE_ENV_DIR",
)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for key, value in (("ai_api_key", ""), ("ai_provider", "openai_compat"),
                       ("ai_model", ""), ("ai_base_url", "")):
        monkeypatch.setattr(settings, key, value)
    return tmp_path


def _item(items: list[dict], item_id: str) -> dict:
    return next(i for i in items if i["id"] == item_id)


def _patch_opend(monkeypatch, *, ok: bool = True, qot_logined: bool = True,
                 error: str | None = None) -> None:
    from app.plugins.moomoo import provider as moomoo_provider
    from app.services import setup_checklist

    # Tests never touch a real OpenD: skip the TCP pre-check and fake get_global_state.
    monkeypatch.setattr(setup_checklist, "_opend_reachable", lambda host, port, timeout=0.3: True)
    monkeypatch.setattr(
        moomoo_provider.MoomooProvider,
        "opend_status",
        lambda self: {"ok": ok, "qot_logined": qot_logined,
                      "market_us": "AFTER_HOURS_END" if ok else None, "error": error},
    )


def _patch_daily_provider(monkeypatch, name: str) -> None:
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: name)


def test_checklist_moomoo_required_when_routed(isolated, monkeypatch):
    from app.services import setup_checklist

    _patch_daily_provider(monkeypatch, "moomoo")
    _patch_opend(monkeypatch)

    item = _item(setup_checklist.build()["items"], "moomoo")

    assert item["group"] == "market"
    assert item["label"] == "moomoo OpenD（美股行情）"
    assert item["level"] == "required"
    assert item["configured"] is True
    assert item["env"] == ["ZENTRADE_MOOMOO_OPEND"]
    assert item["where"] == "settings?tab=data-sources&source=moomoo&highlight=source-detail"
    assert "127.0.0.1:11111" in item["note"]
    assert item["purpose"] and item["impact"]


def test_checklist_moomoo_required_when_daily_unset(isolated, monkeypatch):
    from app.services import setup_checklist

    _patch_daily_provider(monkeypatch, "")
    _patch_opend(monkeypatch)

    item = _item(setup_checklist.build()["items"], "moomoo")

    assert item["level"] == "required"


def test_checklist_moomoo_optional_when_other_daily_source(isolated, monkeypatch):
    from app.services import setup_checklist

    _patch_daily_provider(monkeypatch, "alpaca")
    _patch_opend(monkeypatch)

    item = _item(setup_checklist.build()["items"], "moomoo")

    assert item["level"] == "optional"


def test_checklist_moomoo_unconfigured_when_opend_down(isolated, monkeypatch):
    from app.services import setup_checklist

    _patch_daily_provider(monkeypatch, "moomoo")
    _patch_opend(monkeypatch, ok=False, qot_logined=False, error="Connection refused")

    item = _item(setup_checklist.build()["items"], "moomoo")

    assert item["configured"] is False
    assert "未连接" in item["note"] or "未登录" in item["note"]


def test_checklist_moomoo_unconfigured_when_not_logged_in(isolated, monkeypatch):
    from app.services import setup_checklist

    _patch_daily_provider(monkeypatch, "moomoo")
    _patch_opend(monkeypatch, ok=True, qot_logined=False)

    item = _item(setup_checklist.build()["items"], "moomoo")

    assert item["configured"] is False
    assert "未登录" in item["note"]


def test_checklist_alpaca_optional_when_moomoo_routed(isolated, monkeypatch):
    from app.services import setup_checklist

    _patch_daily_provider(monkeypatch, "moomoo")
    _patch_opend(monkeypatch)

    alpaca = _item(setup_checklist.build()["items"], "alpaca")

    assert alpaca["level"] == "optional"


def test_checklist_alpaca_required_when_daily_not_moomoo(isolated, monkeypatch):
    from app.services import setup_checklist

    _patch_daily_provider(monkeypatch, "")
    _patch_opend(monkeypatch)

    alpaca = _item(setup_checklist.build()["items"], "alpaca")

    assert alpaca["level"] == "required"