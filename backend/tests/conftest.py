"""测试全局基座。

- 冻结 us_clock 日历为工作日近似（cal provider=None）:
  防止任何经 market_clock/facade 的时段判断在测试期打 Alpaca 真网络
  （真 key 存在于本机 env，会真实发请求且在网络不可达时挂死整套测试）。
  需要特定日历语义的测试自行 monkeypatch us_clock._calendar_provider。
"""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import tempfile
from pathlib import Path

# Keep the whole suite off the real data/ folder. Without this, settings.data_dir
# defaults to <repo>/data, and a local run rewrote real kline_daily_enriched
# partitions, job_store and preferences, and appended to the real backend.log.
# This must run before anything imports app.config (settings are built at import),
# and it also reaches subprocess-based tests through the inherited environment.
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="zentrade-test-data-"))
os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)


def _remove_test_data_dir() -> None:
    # The app's log handler keeps <DATA_DIR>/backend.log open, and Windows will not
    # delete an open file, so close logging before removing the folder.
    logging.shutdown()
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


atexit.register(_remove_test_data_dir)

import pytest  # noqa: E402

from app.market_clock import us_clock  # noqa: E402

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
_BUILTIN_DIR = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"
# 完整版独有的内容: 开源核心版导出时排除 (scripts/core_manifest.toml)
FULL_EDITION = (_BUILTIN_DIR / "tv_smc_structure.py").exists() and (_CONFIG_DIR / "swing_rule_history.json").exists()


def pytest_collection_modifyitems(config, items):
    """@pytest.mark.full_edition 的用例只在私有完整版运行; 开源核心版里自动跳过并写明原因。"""
    if FULL_EDITION:
        return
    skip = pytest.mark.skip(reason="full edition only: third-party ports / private replay statistics not in core edition")
    for item in items:
        if item.get_closest_marker("full_edition"):
            item.add_marker(skip)


@pytest.fixture(autouse=True, scope="session")
def _require_isolated_data_dir():
    from app.config import settings

    if Path(settings.data_dir).resolve() != _TEST_DATA_DIR.resolve():
        pytest.exit(
            f"tests would use {settings.data_dir}, not the temp data dir; "
            "app.config was imported before tests/conftest.py set DATA_DIR",
            returncode=3,
        )


@pytest.fixture(autouse=True, scope="session")
def _freeze_market_calendar():
    original = us_clock._calendar_provider
    us_clock._calendar_provider = None
    yield
    us_clock._calendar_provider = original


@pytest.fixture(autouse=True)
def _no_swing_candidate_background_check(request, monkeypatch):
    """0.26.0: 发布策略后会在后台线程起一次卡回放验收 (子进程)。测试里默认不起; 测这个钩子本身的用例
    用 @pytest.mark.real_swing_candidate_check 退出此默认。"""
    if request.node.get_closest_marker("real_swing_candidate_check"):
        yield
        return
    from app.services import swing_candidates
    monkeypatch.setattr(swing_candidates, "schedule_check", lambda data_dir, repo, *, reason: None)
    # 0.30.0: publish goes through request_check, which queues by wall clock; keep tests clock-independent
    monkeypatch.setattr(swing_candidates, "request_check",
                        lambda data_dir, repo, *, reason, now=None, if_idle=False: {"started": False, "queued": False, "running": False})
    yield


@pytest.fixture(autouse=True)
def _no_protection_feed_network(monkeypatch):
    """财报/公司行动源的默认 HTTP 出口在测试中一律拒绝 (本机有真实 key, 不能出网)。"""
    from app.services import corporate_actions, earnings_calendar

    def offline(*a, **kw):
        raise ConnectionError("network disabled in tests")
    monkeypatch.setattr(earnings_calendar, "_http_json", offline)
    monkeypatch.setattr(corporate_actions, "_default_http_get", offline)
    # 证据 v3 研究数据 (退市股/财报历史/SEC) 同样不出网 (2026-10-05)
    from app.services import research_data
    monkeypatch.setattr(research_data, "_jget", offline)
    yield


@pytest.fixture(autouse=True)
def _no_ticker_handoffs(monkeypatch):
    """复权因子测试默认「源端没有代码换主记录」(换主过滤本身见 test_ticker_handoff.py, 直接替换)。

    不替换 foreign_dates_for 本身: 那样会绕过真实的两段查询逻辑; 只把公司行动源换成空结果。
    """
    from app.services import ticker_handoff

    monkeypatch.setattr(ticker_handoff, "fetch_corporate_actions", lambda symbols, start, end, *, types: {})
    yield


@pytest.fixture(autouse=True)
def _fresh_empty_earnings_calendar(request, monkeypatch):
    """既有纸面撮合测试早于财报保护, 默认视为「日历可用且无财报」, 仍验证原行为。

    财报保护本身的测试用 @pytest.mark.real_earnings_calendar 退出此默认。
    """
    if request.node.get_closest_marker("real_earnings_calendar"):
        yield
        return
    from app.services import earnings_calendar
    monkeypatch.setattr(earnings_calendar.EarningsCalendar, "usable", lambda self, now=None: True)
    monkeypatch.setattr(earnings_calendar.EarningsCalendar, "event_for", lambda self, symbol: None)
    yield


@pytest.fixture(autouse=True)
def _fresh_empty_corporate_actions(request, monkeypatch):
    """同上: 既有测试默认「公司行动数据可用且无行动」; 公司行动测试用 real_corporate_actions 退出。"""
    if request.node.get_closest_marker("real_corporate_actions"):
        yield
        return
    from app.services import corporate_actions
    monkeypatch.setattr(corporate_actions.CorporateActionService, "usable", lambda self, now=None: True)
    monkeypatch.setattr(corporate_actions.CorporateActionService, "actions", lambda self: [])
    monkeypatch.setattr(corporate_actions.CorporateActionService, "covers", lambda self, symbol: True)
    yield


@pytest.fixture(autouse=True)
def _no_free_options_network(monkeypatch):
    """0.23.2: 测试里不许真连 Alpaca 免费期权源。"""
    from app.services import alpaca_options

    def _off():
        raise RuntimeError("network disabled in tests")
    monkeypatch.setattr(alpaca_options, "_creds", _off)


@pytest.fixture(autouse=True)
def _no_swing_premarket_network(request, monkeypatch):
    """0.27.0: 盘前越止损检查 (SIP 30 分钟K) 与机会板盘前量角标在测试里不出网。
    默认 = SIP 没有盘前成交 (检查记 source=none, 不作废); 测检查本身的用例直接给 run(sip=...) 传桩。"""
    from app.services import swing_desk, swing_pm_check
    monkeypatch.setattr(swing_pm_check, "sip_last_premarket", lambda symbols, day: {})
    monkeypatch.setattr(swing_desk, "_premarket_volume", lambda symbols, now: {})
    yield
