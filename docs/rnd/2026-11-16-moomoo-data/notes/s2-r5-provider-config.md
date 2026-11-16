# Data provider config and selection

Scope: how a DATA SOURCE is configured, enabled and chosen in ZenTrade. Read-only recon; claims cited by file:line. Repo is at 2026-11-16.

## Credentials storage

- All external "needs a key" credentials resolve through one place: `backend/app/credentials.py` (module docstring `credentials.py:1-8`). Lookup order: (1) `data/user_data/secrets.json` field, (2) process env var `SecretSpec.env`, (3) optional `ZENTRADE_ENV_DIR/<file>` (`credentials.py:52-65`).
- Secrets file: `settings.data_dir / "user_data" / "secrets.json"`, mode 0600; docstring says priority `secrets.json > .env > empty` (`backend/app/secrets_store.py:1-6`, `_path()` at `secrets_store.py:21-25`). `.env` is never rewritten by UI (`secrets_store.py:5-6`).
- Alpaca specifically: packed spec `SecretSpec("ZENTRADE_ALPACA_KEY", "alpaca_api_key")` = `KEY_ID:SECRET` written from UI (`credentials.py:26`); fallback env specs `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`, optional file `alpaca.env` (`credentials.py:27-28`). `alpaca()` prefers the packed UI value, then the two env vars (`credentials.py:70-81`).
- Generic plugin keys: UI save is probe-then-store, writing `{plugin}_api_key` into secrets.json (`backend/app/api/settings.py:758-777`); only a masked form is ever returned (`custom/loader.py:94-110`, `secrets_store.py:145`).
- `.env` path is `TICKFLOW_ENV_FILE` or project-root `.env` (`backend/app/config.py:94-101`), loaded by pydantic-settings for `Settings` (`config.py:103-108`). `credentials.py` reads `os.environ`, so `.env` reaches it via `uvicorn --env-file .env` in dev (`dev.sh:44-48`) or compose `env_file: .env` (`docker-compose.yml:18-19`).
- No data-provider credentials are fields of `Settings` (`backend/app/config.py:111-180`: only AI/server/auth/data paths). `grep -n -i alpaca backend/app/api/settings.py` → no match. NOT VERIFIED: any sqlite storage of keys (none found in reviewed files; keys are JSON/env).

## Provider selection / registry

- Registry = in-memory `_PROVIDERS` (`backend/app/data_providers/custom/loader.py:35-40`). Loaded by `load_all()`: scans `data/data_sources/*.yaml(.yml)` plus built-in plugins (`loader.py:47-72`). Built-in plugins come from `app/plugins/*/plugin.yaml`, availability decided by manifest `check`, usable ones registered with `builtin=True` (`loader.py:544-607`); `names()` returns registry keys (`loader.py:297-298`).
- Dataset capability of a source is declared, not inferred: `provider_has_dataset()` reads plugin manifest `datasets:` or YAML `provider.config.datasets` (`loader.py:327-340`).
- There is **no priority list / automatic fallback chain**. Each dataset ("capability") has its own independent preference field and exactly one effective source; empty string = unconfigured (`backend/app/data_providers/capabilities.py:1-19`, registry `capabilities.py:65-137`). The old "follow daily" special value is gone (`capabilities.py:73-77`).
- Preference fields: `daily_data_provider`, `adj_factor_provider`, `minute_data_provider`, `full_minute_data_provider`, `realtime_data_provider`, `financial_data_provider` (`settings.py:576-582`), stored in `data/user_data/preferences.json` (`backend/app/services/preferences.py:3`, `_path()` `preferences.py:25-27`), set via `PUT /api/settings/preferences/data-providers` (`settings.py:945-969`).
- Validation on read: a stored value not in `_ALLOWED_DATA_PROVIDERS | custom_sources.names()` becomes `""` (`preferences.py:223`, `267-298`). Exception: `financial` defaults to `sec_edgar` when that plugin is registered (`preferences.py:300-306`).
- Runtime consumption (fail-closed, no provider→provider fallback): daily uses the configured source only if it declares `daily`, otherwise the legacy TickFlow leg is retired and the batch sync warns/no-ops (`backend/app/services/kline_sync.py:256-263`, `197-201`). Minute `_resolve_minute_provider` returns `(None, True)` on unconfigured/exception → silent degrade; "自定义源失败即本轮无数据" (`kline_sync.py:709-740`, `851`). Realtime mode is `none` unless a `realtime` dataset is configured (`backend/app/services/quote_service.py:436-447`).
- Capability matrix (`GET /api/settings/capability-matrix`, `settings.py:733-751`) merges registry + declared sources + current prefs into per-capability `candidates` (available) / `pending` (unavailable, with reason) and `usable` (`capabilities.py:159-201`).

## Setup checklist + UI

- Backend: `GET /api/settings/setup-checklist` returns `setup_checklist.build()` (`settings.py:65-69`), read-only, no key text (`backend/app/services/setup_checklist.py:1-13`).
- Alpaca item: id `alpaca`, group `market`, level `required`, configured = both key id and secret present (source: ui/env/env_dir), where-to-configure `settings?tab=data-sources&source=alpaca&highlight=source-detail`, env names `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` (`setup_checklist.py:101-114`). TickFlow item is `optional` (`setup_checklist.py:115-121`); Finnhub/SEC UA recommended (`124-139`); QVeris/free sources optional (`141-153`); `summary.required_missing` / `recommended_missing` / `ready` (`setup_checklist.py:165-172`).
- UI checklist: one row per item with anchor `setup-<id>`, source label and env-var tooltip (`frontend/src/components/SetupChecklist.tsx:6`, `30-31`, `83-95`, `130-132`). Banner dismissal stored in preferences (`settings.py:76-83`).
- Data-sources page: per-capability routing cards; clicking a candidate chip switches that capability's provider; pending sources greyed with reason (`frontend/src/pages/settings/DataSources.tsx:105-211`); persisted through prefs (`DataSources.tsx:212-280`).
- Alpaca card: `AlpacaDetail` shows "服务中" when routed+available, or "已设为数据源 · Key 未配置" when routed but unavailable (`DataSources.tsx:1049-1068`), and embeds `PluginKeyConfig` with hint "格式 KEY_ID:SECRET" and env fallback label (`DataSources.tsx:1080-1084`). Frontend display fallback is `'alpaca'` for unset prefs (`DataSources.tsx:101-106`, `551`, `567-575`), which differs from backend effective `""` (unconfigured).

## Existing futu/moomoo code

- None. `grep -rn -i "futu\|moomoo" backend/app` produces only false positives: `from __future__` imports and "future" loop names (e.g. `backend/app/api/kline.py:1513`, `backend/app/api/pipeline.py:5`, `backend/app/backtest/engine.py:932,1143-1148,1177-1179`). No `futu`/`moomoo` plugin dir: `backend/app/plugins/` contains `alpaca, hyperliquid, qveris, sec_edgar, tickflow`; no credential spec in `credentials.py:26-31`; no dataset entry in `capabilities.py:65-137`; no preference field in `settings.py:576-582`.

## Open questions

- No fallback chain exists, so a second source for any dataset is only used when manually routed; would moomoo need to replace a dataset or add a new capability id? NOT VERIFIED: intended UX for multi-source failover.
- How should moomoo auth (account/password/host, possibly per-account) fit `SecretSpec` + `probe_api_key`? `probe_plugin_key` requires module-level `probe_api_key(key)` (`loader.py:127-151`), which may not map to Futu OpenD session config.
- Desktop/frozen builds: `ZENTRADE_ENV_DIR` and `.env` injection paths differ (`config.py:29-58`, `dev.sh:44-48`); NOT VERIFIED: how a moomoo plugin key would be entered on the frozen desktop build beyond the generic plugin-key UI.