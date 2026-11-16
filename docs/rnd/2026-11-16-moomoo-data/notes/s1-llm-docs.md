# LLM provider docs check

Fetched 2026-11-16. Raw pages saved under `D:/Desktop/rnd-scratch-2026-11-16/s1-docs/`.

## Surplus Intelligence (base URL, auth, model ids, quick-start model, models endpoint)

- **Base URL (host):** `https://api.surplusintelligence.ai` — https://www.surplusintelligence.ai/llms.txt lines 13-16: "## Base URL ... https://api.surplusintelligence.ai"; "API calls go to the `api.` host."
- **Exact OpenAI-compatible base URL:** `https://api.surplusintelligence.ai/v1` — llms.txt:773: `client = OpenAI(api_key="inf_...", base_url="https://api.surplusintelligence.ai/v1")`; buyer quickstart (https://www.surplusintelligence.ai/docs/getting-started/buyer-quickstart): "API Base URL: https://api.surplusintelligence.ai/v1".
- **Auth header:** `Authorization: Bearer inf_...` — llms.txt:618: "Authorization: Bearer inf_0f8c2a1b4d6e47f9a3c5b7d9e1f24680" (buyer key prefix `inf_`). Anthropic skin (llms.txt:974) also accepts `x-api-key`.
- **Model ids shown in docs:** the docs deliberately print no static list — llms.txt:1193: "`GET /v1/models` — the live catalog. Read this rather than trusting any list in this file."
  - Individually shown: `llama-3.3-70b` (llms.txt:383, 773), `claude-opus-4.8` (llms.txt:984), `gpt-4o-mini` (llms.txt:985), `claude-sonnet-4-5` (llms.txt:1008).
  - Web docs pages add: `claude-opus-4.6` (buyer quickstart curl + API-reference models example, https://www.surplusintelligence.ai/docs/api-reference/models), `claude-opus-5`, `gemini-3-flash` / `gemini-3-flash-preview` (API-reference models page).
- **Quick-start example model:** llms.txt agent quickstart uses `llama-3.3-70b` (llms.txt:773). Web Buyer Quickstart uses `claude-opus-4.6` — https://www.surplusintelligence.ai/docs/getting-started/buyer-quickstart: `"model": "claude-opus-4.6"`.
- **Models endpoint:** `GET https://api.surplusintelligence.ai/v1/models` — https://www.surplusintelligence.ai/docs/api-reference/models: "Returns all available models with pricing, context lengths, and capabilities. OpenRouter-compatible format. No authentication required."
  - Called anonymously (no key) on 2026-11-16: HTTP 200, 506 model ids. Examples of first ids: `openai-gpt-oss-120b`, `minimax-m2.1`, `deepseek-v4-flash`, `gpt-5-mini`, `claude-opus-4.6`, `gpt-5.5`, `gemini-3.1-flash-lite`.
  - DeepSeek-named catalog ids include: `deepseek-v4-pro`, `deepseek-v4-pro-0813`, `deepseek-v4.1-flash`, `deepseek-r1`, `deepseek-v3.2`.

## DeepSeek (base URLs, model ids, is deepseek-v4-pro current?)

- **Base URL, OpenAI format:** `https://api.deepseek.com` — https://api-docs.deepseek.com/quick_start/pricing: "BASE URL (OpenAI Format) https://api.deepseek.com"; the quickstart code uses `base_url = "https://api.deepseek.com"`.
- **Base URL, Anthropic format:** `https://api.deepseek.com/anthropic` — same pricing table: "BASE URL (Anthropic Format) https://api.deepseek.com/anthropic".
- **Request path shown:** curl example posts to `https://api.deepseek.com/chat/completions` (no `/v1` segment) — https://api-docs.deepseek.com/ "Invoke The Chat API".
- **Current model ids:** `deepseek-flash` and `deepseek-v4-pro` — https://api-docs.deepseek.com/quick_start/pricing: "MODEL deepseek-flash (1) deepseek-v4-pro"; https://api-docs.deepseek.com/ quick start table: "model deepseek-flash (1) deepseek-v4-pro".
- **Legacy ids (accepted, retired):** `deepseek-v4-flash`, `deepseek-v4-flash-vision-exp` — pricing footnote (1): "The legacy names deepseek-v4-flash and deepseek-v4-flash-vision-exp are still accepted, but the corresponding models have been retired... served by DeepSeek-V4.1-Flash-1017 and billed at the Flash price."
- **Is `deepseek-v4-pro` current?** YES — it is one of the two columns in the live "Models & Pricing" table (fetched 2026-11-16), model version "DeepSeek-V4-Pro-0813".

## AI.tsx DeepSeek preset vs docs: match / mismatch

- `frontend/src/pages/settings/AI.tsx:60`:
  `{ label: 'DeepSeek', url: 'https://api.deepseek.com', model: 'deepseek-v4-pro', website: 'https://www.deepseek.com/', websiteLabel: 'deepseek.com', description: 'DeepSeek 官方接口（OpenAI 兼容）' },`
- **URL: MATCH.** `url: 'https://api.deepseek.com'` equals the docs' OpenAI-format base URL exactly (no trailing `/v1`).
- **Model: MATCH.** `model: 'deepseek-v4-pro'` is a current id in the docs pricing table.
- Caveat: docs' own curl path is `/chat/completions` without `/v1`; whether this frontend appends `/v1/chat/completions` to `baseUrl` was NOT VERIFIED (out of scope, no code read).
- **TypeScript shape** — `frontend/src/pages/settings/AI.tsx:55`:
  `type AiPreset = { label: string; provider?: string; url: string; model: string; codexCommand?: string; website: string; websiteLabel: string; description: ReactNode; custom?: boolean }`
  (array declared `const PRESETS: AiPreset[] = [` at line 57.)

## Unanswered

- Surplus has no static documented model list by design; only live `GET /v1/models` (506 ids at fetch time) — no per-model doc page found (`/docs/models` and `/docs/getting-started/index` returned HTTP 404).
- Whether `frontend` appends `/v1` when calling DeepSeek — NOT VERIFIED.
- DeepSeek models-list endpoint (`GET /models`) was not fetched; not required by the task.
- Surplus pricing/limits are dynamic and were not enumerated beyond what the docs state ("GET /v1/prices" is the source of truth).