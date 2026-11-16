# LLM presets: DeepSeek + Surplus Intelligence — short design (step 1 of 5)

Date: 2026-11-16. Size: small change. The short design was shown to the user at 16:20, with no objection;
the build started at 17:10 as scheduled.

**Intent.** The user wants DeepSeek and Surplus Intelligence selectable as LLM providers in ZenTrade.

**Current state (verified).**
- Both providers are OpenAI-compatible. ZenTrade's AI client (`backend/app/services/ai_provider.py`) already
  takes any base URL plus model.
- The Settings → AI page offers one-click presets (`frontend/src/pages/settings/AI.tsx:57-66`).
- DeepSeek is already there: `AI.tsx:60`, `https://api.deepseek.com`, `deepseek-v4-pro`. Both values match
  DeepSeek's docs as fetched on 2026-11-16 (`docs/rnd/2026-11-16-moomoo-data/notes/s1-llm-docs.md`).

**Change.** Add one PRESETS entry after DeepSeek:

```ts
{ label: 'Surplus Intelligence', url: 'https://api.surplusintelligence.ai/v1', model: 'deepseek-v4.1-flash', website: 'https://www.surplusintelligence.ai/', websiteLabel: 'surplusintelligence.ai', description: 'Surplus Intelligence 聚合接口（OpenAI 兼容）· 模型见 /v1/models' },
```

- **Base URL:** from the docs (`llms.txt:773`, buyer quickstart).
- **Model id:**
  - The docs publish no default; they point to the live `GET /v1/models` catalogue (506 ids, fetched
    without a key on 2026-11-16).
  - The docs' own quick-start id `llama-3.3-70b` is **not** in that catalogue (it lists
    `llama-3.3-70b-instruct`), so it was not used.
  - `deepseek-v4.1-flash` is in the catalogue. It is also the model the user's own pi agent runs through
    Surplus.
  - The user can type any other catalogue id in the model field.

**Approaches.** Only one sensible way: the preset list is the existing mechanism. A backend-side provider
type isn't needed because both APIs are OpenAI-compatible.

**Non-goals.**
- No backend change.
- No change to the existing DeepSeek-V4 "thinking off" handling (`ai_provider.py:855-886`). That handling
  only covers `deepseek-v4-flash`/`deepseek-v4-pro` on `api.deepseek.com`. DeepSeek's new id
  `deepseek-flash`, and DeepSeek models reached via Surplus, are not covered. This is flagged to the user,
  not changed.

**Verification.**
- Frontend type check.
- Existing AI settings test (`AI.tasks.test.tsx`).

**Untested.**
- A live chat call: it needs the user's API keys.
