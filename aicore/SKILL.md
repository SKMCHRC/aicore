---
name: aicore
description: >
  Mandatory usage guide for the `aicore` package — read it BEFORE writing or
  changing any code that uses aicore. Covers: resolving LLM model config + live
  API key (get_model_config); the mandatory shared GET /health endpoint;
  @ai_history audit tracking and the step-wise input/output + per-call cost
  structure for AI (LLM) endpoints; database access ONLY through the DB Access
  API (query/execute/custom_query/custom_execute — never a direct database
  connection, no Oracle Instant Client); error handling, migration steps and
  the PR review checklist. Use when adding/changing an LLM endpoint
  (Gemini/Ollama) in a FastAPI app, replacing hardcoded model config/keys,
  adding AI request history, running DB queries, or reviewing an aicore app.
---

# aicore — usage guide & app rules

`aicore` is the shared Python library for AICORE apps. It gives every app:

1. **`get_model_config(purpose)`** — the ONE configured LLM (model, provider, URL, params) **plus the live paid API key**.
2. **`@ai_history`** — one audit row per AI endpoint call (`PROCESSING → COMPLETED | ERROR`) in the audit-history table.
3. **`add_health_endpoint`** — the **mandatory** shared `GET /health`.
4. **DB Access API client** — `query / execute / custom_query / custom_execute`. **The only way an app touches a database.**

**MUST / MUST NOT** below are mandatory (reviewers reject the app without
them); **SHOULD** is a strong default. Read this whole file before writing
code that uses aicore.

> **Status of enforcement.** The library *enforces* today: `task_type`
> present, the 5 audit fields, and the `{"input", "output"}` return shape.
> It does **not yet** enforce the step-wise structure (§6) or cost fields (§7)
> — those are the app's responsibility until a helper ships. Treat them as
> mandatory anyway; they make history usable for audit and billing.

---

## 1. Setup

```bash
pip install git+https://github.com/SKMCHRC/aicore.git      # `requests` comes with it
pip install fastapi                                        # your web framework, if not already
```

`.env` at the project root (or point `LIB_AI_ENV_FILE` at it). Real environment variables win over the file.

```env
DB_API_URL=http://<api-host>:<port>     # DB Access API
DB_ACCESS_KEY=<your-access-key>                 # per-application key — SECRET, never commit
APP_ID=13                            # YOUR registry app id — REQUIRED; missing → AppIdMissingError
# optional: DB_API_TIMEOUT=200  DB_API_RETRIES=4
# dev only: TRACKING_ENABLED=false     (NEVER in production)
```

That is all an app needs. **No Oracle credentials, no Instant Client** (§8).

Verify:
```bash
python -m aicore GENERATE_MDT       # resolves model config + live key; non-zero exit = data/env problem
```

Import style — one line covers everything:
```python
from aicore import (get_model_config, ai_history, add_health_endpoint,
                    query, execute, custom_query, custom_execute, DbApiError)
```
Don't import internals (`_Record`, `_hist_*`, `aicore.db`).

---

## 2. Mandatory checklist (copy into every PR)

- [ ] **Model config via `get_model_config(purpose)`** — no hardcoded model names, keys, URLs, temperatures. Called per request, never cached at import.
- [ ] **`GET /health` wired** with `add_health_endpoint(app)` — it probes exactly the URLs `APP_ID` uses, from the DB (§4).
- [ ] **Every AI endpoint** is `@route` → `@ai_history(task_type)` → function (§5).
- [ ] **Audit fields** `mrno, requested_by, org_id, zon_id, loc_id` reach every AI endpoint (§5.3).
- [ ] **AI endpoints only (`@ai_history`):** return `{"input": {...steps}, "output": {...steps + cost}}` — step-wise, one entry per step (§6). Non-AI endpoints: no required shape.
- [ ] **Cost recorded** for every Gemini (paid) call in an AI endpoint; Ollama recorded as `0` (§7).
- [ ] **No direct database connection** — no `oracledb`, no Instant Client, no DB credentials; all data access through `query()/execute()` (§8).
- [ ] **No Oracle Instant Client folder and no Oracle layers in the Dockerfile** (§8.3).
- [ ] **No secrets** in input/output/logs (§9).
- [ ] `TRACKING_ENABLED` is **not** `false` in production.
- [ ] Tested: missing-field request → 400; good request → `PROCESSING → COMPLETED` row; LLM host down → `/health` 503.

---

## 3. `get_model_config(purpose)`

```python
cfg = get_model_config(purpose="GENERATE_MDT")   # app id = APP_ID from .env
```

| key | meaning |
|---|---|
| `app_name`, `app_id`, `purpose`, `label` | identity ("app / purpose") |
| `model_id`, `model_name`, `model_type` | the LLM (e.g. `llama3.1`, `gemini-2.5-flash`) |
| `provider_id`, `provider` | `OLLAMA` / `GEMINI` / … |
| `url` | Ollama base URL (None for GEMINI) |
| `temperature`, `seed`, `max_retries`, `keep_alive`, `context_size`, `num_predict` | inference params |
| `active`, `remarks` | registry metadata |
| `is_ollama`, `is_gemini` | convenience booleans |
| `api_key` | **the LIVE PAID+active API key** (None if provider has none yet) |
| `api_key_meta` | `{llm_api_id, email_id, api_type, max_hit, api_key:"***"}` |

Rules:
- Call it **per request / per step** (one cheap API call) — never cache keys at import time; keys rotate.
- One `purpose` per endpoint purpose (`ASK_QA` vs `SUMMARIZE`) — don't reuse one purpose for different models.
- Handle `ModelLookupError`, `AppNotRegisteredError`, `AmbiguousModelError` at the API layer — they are platform data problems: surface **503**, not 500.
- Never log `cfg["api_key"]`; log `cfg["api_key_meta"]` (redacted) instead.

Calling the model with the resolved config:
```python
if cfg["is_ollama"]:
    resp = httpx.post(f"{cfg['url']}/api/generate",
                      json={"model": cfg["model_name"], "prompt": prompt, "stream": False,
                            "options": {"temperature": cfg["temperature"]}})
elif cfg["is_gemini"]:
    resp = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{cfg['model_name']}:generateContent",
        headers={"x-goog-api-key": cfg["api_key"]},
        json={"contents": [{"parts": [{"text": prompt}]}]})
```

---

## 4. Health check — every app, no exceptions

```python
from aicore import add_health_endpoint

app = FastAPI()
add_health_endpoint(app)     # app id from APP_ID in .env — nothing else to configure
```

Rules:

1. **MUST** exist on every app, even if the app has no AI endpoint yet 
2. **What is probed is automatic:** exactly the URLs of the ACTIVE models of the app's ACTIVE tasks (saved query `aicore_app_provider_urls`, param `app_id`). Add a model to your app in the registry and `/health` covers it — no code change. Models without a URL are ignored; if the app has no URL at all, `/health` just answers `healthy`.
3. **MUST** use the library's endpoint. Don't hand-roll `/health`, don't wrap it in `@ai_history`, don't require the audit fields on it.
4. **MUST** return **503** when any probed URL is unreachable, or when the lookup itself fails (verify by pointing the URL at a dead host). Overall `healthy` ⇔ app up **and** every probed URL reachable.
5. What it proves: *this server can reach the LLM host* (any HTTP status = reachable). It does **not** validate keys, quotas, or model availability — don't treat green as "AI works". Use `get_model_config` + a real call in your own tests for that.
6. The app id comes from `APP_ID` in `.env`; if it is missing, `add_health_endpoint` raises `AppIdMissingError` at startup. Results are cached 60 s. There is **no DB check** in `/health` — apps never connect to a database.

Response shape (fixed — monitors parse it):

```jsonc
// 200
{"status":"healthy","checks":{"app":{"status":"ok"},
  "llm":[{"provider":"ollama","url":"http://<ollama-host>:11434","reachable":true,"http_status":200,"latency_ms":12}]}}   // "llm":[] = nothing to check
// 503
{"status":"unhealthy","checks":{"llm":[{"provider":"ollama","reachable":false,"detail":"URLError: ..."}]}}
```

---

## 5. `@ai_history` — where and how

### 5.1 What it is for
One audit row in the audit-history table **per endpoint call**
(not per LLM call): `PROCESSING → COMPLETED | ERROR`, with `INPUT_JSON` and
`OUTPUT_JSON`.

### 5.2 Where to use it
| Use on | Don't use on |
|---|---|
| Every HTTP endpoint that calls an LLM (Gemini / Ollama / any provider) or produces an AI result | `/health` |
| Sync or async handlers | Helpers, utilities, background functions with no request (they fail the audit-field check — by design) |
| Endpoints that chain several LLM calls (still **one** decorator, steps inside) | Each inner LLM call (never decorate per step) |

### 5.3 Placement and arguments
```python
@app.post("/summarize")                                        # 1. route (outermost)
@ai_history(task_type="SUMMARIZE_NOTE")                       # 2. aicore
async def summarize(req: Req, request: Request):               # 3. handler
    ...
```
- `app_id` — optional: defaults to `APP_ID` in `.env` (missing → `AppIdMissingError` at import). Pass it explicitly only to override.
- `task_type` — **non-empty, stable, UPPER_SNAKE, unique per endpoint purpose**. Empty → `ValueError` at import. Never build it dynamically from user input.
- Pass `request: Request` in the signature so headers/query are visible.
- **Audit fields (no opt-out):** `mrno, requested_by, org_id, zon_id, loc_id` must arrive via Form, JSON body, query, or headers `x-mr-no / x-user / x-org / x-zon / x-loc`. Missing → HTTP 400 listing them, handler never runs, an `ERROR` row is written.

### 5.4 Don'ts
- **Never** catch `AiHistoryFormatError` / `AIHistoryContextError` to "make it work". A contract breach is a bug in the endpoint.
- **Never** `TRACKING_ENABLED=false` in production.
- Don't touch library internals (`_Record`, `_hist_*`).
- Tracking-DB failures are swallowed by the library; they will not break your endpoint — but also won't tell you. Watch logger `aicore.history`.

---

## 6. Return structure — step-wise input and output

> **Scope: AI endpoints only.** This structure (and the cost rules in §7)
> applies **only to endpoints decorated with `@ai_history`** — i.e. endpoints
> that make LLM calls. For every other endpoint (CRUD, lookups, uploads,
> `/health`, internal utilities) there is **no required input/output shape**;
> return whatever the client needs. Don't add `steps`/`cost` to them and
> don't decorate them with `@ai_history` just to get a history row.

An `@ai_history` handler returns:

```python
return {
    "input":  {...},   # what went IN, step by step
    "output": {...},   # what came OUT, step by step, plus cost
    # optional extra client-facing keys (not stored)
}
```

### 6.1 Definitions
- A **step** is one unit of work in the request: an LLM call, or a non-LLM stage (OCR, DB lookup, rule check, parsing).
- Steps are numbered `1..N` in execution order. **`input.steps[i].step` and `output.steps[i].step` must match** — that is how history is read back.
- A single-call endpoint still has exactly **one** step. Never skip the structure because "it's only one call".
- Retries and fallbacks (e.g. Gemini fails → Ollama) are **separate steps** (so a retried paid call is visible and costed).
- Parallel calls: give each its own `step` number (assign in submission order).

### 6.2 `input`

```jsonc
"input": {
  "request": { /* short summary of what the client sent — ids, filenames, flags; NOT the full payload */ },
  "steps": [
    {
      "step": 1,
      "name": "extract_entities",            // stable snake_case
      "type": "llm",                          // "llm" | "tool"
      "provider": "GEMINI",                   // from cfg["provider"]; null for tool
      "model": "gemini-2.5-flash",            // from cfg["model_name"]; null for tool
      "purpose": "EXTRACT_ENTITIES",          // purpose passed to get_model_config
      "params": {"temperature": 0.2},         // inference params actually used
      "input": "<prompt / payload sent to this step>"
    },
    { "step": 2, "name": "summarize", "type": "llm", "provider": "OLLAMA",
      "model": "llama3.1", "purpose": "SUMMARIZE", "params": {"temperature": 0.1},
      "input": "<prompt for step 2, may embed step-1 output>" }
  ]
}
```

### 6.3 `output`

```jsonc
"output": {
  "cost": {                                   // ← keep this key FIRST-ish; see 6.5
    "currency": "USD",
    "total": 0.000412,                        // sum of steps[].cost.amount
    "by_provider": {"GEMINI": 0.000412, "OLLAMA": 0.0},
    "total_tokens": 1830,
    "pricing_as_of": "2026-11-25"
  },
  "steps": [
    {
      "step": 1,
      "name": "extract_entities",
      "type": "llm",
      "status": "ok",                         // "ok" | "error" | "skipped"
      "provider": "GEMINI",
      "model": "gemini-2.5-flash",
      "output": "<what this step produced>",
      "usage": {"input_tokens": 1200, "output_tokens": 180, "thinking_tokens": 40, "total_tokens": 1420},
      "cost": {"currency": "USD", "amount": 0.000412, "billable": true},
      "latency_ms": 840,
      "error": null
    },
    {
      "step": 2, "name": "summarize", "type": "llm", "status": "ok",
      "provider": "OLLAMA", "model": "llama3.1", "output": "<...>",
      "usage": {"input_tokens": 300, "output_tokens": 110, "thinking_tokens": 0, "total_tokens": 410},
      "cost": {"currency": "USD", "amount": 0.0, "billable": false},
      "latency_ms": 2100, "error": null
    }
  ]
}
```

Per-step output fields:

| field | rule |
|---|---|
| `step`, `name`, `type`, `status` | always |
| `provider`, `model` | always for `llm`; `null` for `tool` |
| `output` | the step's result (or `null` on error) |
| `usage` | always for `llm` (tokens as reported by the provider); omit or zeros for `tool` |
| `cost` | always for `llm` (§7); `{"amount": 0.0, "billable": false}` for `tool` |
| `latency_ms` | always |
| `error` | `null` on ok; `{"type","message"}` on error. Never include stack traces or keys |

### 6.4 Chained steps
Step N's `input` stores exactly what was sent to the model in step N (including
anything derived from step N-1). Don't store only the "original" input and
infer the rest — an auditor must be able to replay each step from `input.steps[N]`.

### 6.5 Size limits (this bites!)
- Each of `INPUT_JSON` / `OUTPUT_JSON` is capped at **15,000 characters** and
  **truncated mid-string** (the JSON is then no longer parseable) — keys are
  serialized **sorted alphabetically**, so what sorts last gets cut first.
- Therefore: put totals under **`cost`** (sorts before `steps`), and keep per-step
  `input`/`output` compact. For large documents/images/audio store a reference
  (id, filename, size, sha256) or a short preview (first ~500 chars) instead of the
  full body. **Never lose the cost data to truncation.**
- Fields with names such as `token`, `api_key`, `secret`, `password`, `authorization` are masked to `"***"` — keep token *counts* in `*_tokens` keys (e.g. `input_tokens`), not a bare `token` key.

### 6.6 Failures
- **A step fails but the request continues** (fallback, partial result): record the step with `status:"error"` + `error`, keep going, return normally. Failed paid calls that were still billed keep their `usage`/`cost`.
- **The request fails entirely**: raise (e.g. `HTTPException`). The library then writes `ERROR` with only `{"error", "exception"}` — **the step trail and cost of calls already made are lost from history**. If cost accountability matters for failed requests, catch at the endpoint boundary, return a structured error response *with* the steps (`"output": {"cost": ..., "steps": [...], "failed": true}`), and set a 4xx/5xx on the HTTP response yourself. (Library limitation; a helper is planned.)

---

## 7. Cost — every paid call

### 7.1 Rules
1. **Every Gemini call MUST carry `usage` + `cost`** in its output step. This includes retries, fallbacks, and failed-but-billed calls.
2. **Ollama is free**: `cost = {"amount": 0.0, "billable": false}`. Still record `usage` (token counts) and `latency_ms`.
3. Any other paid provider added later follows the Gemini rule; only self-hosted/local models are `billable:false`.
4. Cost is computed **from the provider's reported usage**, never estimated from prompt length when real counts exist.
5. `output.cost.total` MUST equal the sum of `steps[].cost.amount`. Currency is `USD`. Use full precision (don't round to cents — a request costs fractions of a cent).
6. Pricing lives in **one place per app** (a `pricing.py` constant table with `pricing_as_of`), **not** inline in endpoints. When Google changes prices, update that one table. **Verify the numbers against Google's current price list; the ones below are illustrative.**

### 7.2 Where the token counts come from
| Provider | Response field → `usage` |
|---|---|
| Gemini | `usageMetadata.promptTokenCount` → `input_tokens`; `candidatesTokenCount` → `output_tokens`; `thoughtsTokenCount` → `thinking_tokens`; `totalTokenCount` → `total_tokens` |
| Ollama | `prompt_eval_count` → `input_tokens`; `eval_count` → `output_tokens`; thinking = 0 |

Gemini bills **thinking tokens at the output rate** — include them in cost. Audio/image input may price differently from text; price per modality if the model's table distinguishes it.

### 7.3 Reference helper (put in your app's `pricing.py`)

```python
from datetime import date

PRICING_AS_OF = "2026-11-25"          # update whenever the table is re-verified
# USD per 1,000,000 tokens — ILLUSTRATIVE, verify against the provider's price list
GEMINI_PRICES = {
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    # add every model your registry can return for this app
}

def step_cost(provider: str, model: str, usage: dict) -> dict:
    if provider.upper() == "OLLAMA":
        return {"currency": "USD", "amount": 0.0, "billable": False}
    price = GEMINI_PRICES.get(model)
    if price is None:
        # NEVER silently report 0 for a paid model — surface it
        return {"currency": "USD", "amount": None, "billable": True,
                "note": f"no price configured for {model}"}
    out_tokens = usage["output_tokens"] + usage.get("thinking_tokens", 0)
    amount = (usage["input_tokens"] * price["input"] + out_tokens * price["output"]) / 1_000_000
    return {"currency": "USD", "amount": amount, "billable": True}
```

An unknown paid model must produce `amount: null` + a note — **never `0`**, which would look like "free".
Totals skip `null` and set `output.cost.incomplete = true` so it's visible.

---

## 8. Database access — API only, never a direct connection

**Apps MUST NOT connect to any database directly.** All data access goes
through the DB Access API, using the `aicore` client. The API runs **saved,
named queries** that an admin defines on its dashboard; your app sends the
query *name* + parameters over HTTP.

### 8.1 Forbidden
- `import oracledb` / `cx_Oracle` / SQLAlchemy / any DB driver in app code.
- `aicore.db` (direct Oracle layer — internal to the library, not for apps).
- `DB_HOST`, `DB_PORT`, `DB_SERVICE_NAME`, `DB_USER`, `DB_PASSWORD` in your `.env` or deployment config. If they're there, delete them.
- `ORACLE_CLIENT_LIB_DIR` and any thick-mode setup.
- Connection pools, `SELECT 1 FROM DUAL` pings, or SQL strings built with f-strings/concatenation.

### 8.2 How to do it

```env
# .env — the ONLY database-related settings an app has
DB_API_URL=http://<api-host>:<port>
DB_ACCESS_KEY=<your-access-key>            # per-application key; secret, never commit
APP_ID=13                                  # your registry app id
# optional: DB_API_TIMEOUT=200  DB_API_RETRIES=4
```

```python
from aicore import query, execute, DbApiError

# READ — a saved query by NAME + params
rows = query("patient_by_mrno", {"mrno": "1234567"}).dicts()    # list[dict]
row  = query("patient_by_mrno", mrno="1234567").first           # kwargs also work
# result: .columns  .rows  .first  .dicts()  .has_more

# WRITE — returns rows affected (already committed; there is no commit() to call)
n = execute("add_note", {"mrno": "1234567", "note": "Vitals stable"})

# Errors — one exception type
try:
    rows = query("patient_by_mrno", {"mrno": mrno}).dicts()
except DbApiError as e:
    # e.status  e.code  e.message  e.request_id
    ...
```

Rules:
1. **Need a new query?** Ask the admin to create a named saved query on the DB Access API dashboard, then call it by name. Don't ask for DB credentials.
2. **Parameters only** — pass values in the params dict (`:binds` on the server). Never build SQL text from user input.
3. `custom_query(sql, params)` / `custom_execute(sql, params)` work **only if the admin enabled "Custom read" / "Custom write" on your key**. Prefer saved queries — full rules in §8.4.
4. Handle `DbApiError`; branch on `e.code` (`forbidden`, `invalid_request`, `query_timeout`, ...). Log `e.request_id` — it's what the admin needs to trace the call. Map failures to 5xx/503, don't leak raw messages to clients.
5. Retry behaviour is built in: 429/503 are retried with backoff; **writes are never retried after a network error or timeout** (they may have committed). Don't add your own blind retry around `execute`.
6. Large results: check `.has_more` — a query may return a capped page.
7. Don't open a transaction across calls: each API call is its own DB session. Design writes as one saved query per atomic operation (ask the admin for a combined query if you need two statements to succeed together).
8. Model lookups (`get_model_config`) and history (`@ai_history`) already use this API internally — they need **only** `DB_API_URL` + `DB_ACCESS_KEY`, nothing else.

### 8.3 Cleanup when migrating an existing app
- [ ] Delete the **Oracle Instant Client directory** from the repo/project (e.g. `instantclient_*`) and any `ORACLE_CLIENT_LIB_DIR` reference.
- [ ] **Dockerfile:** remove every Instant Client step — downloading/unzipping `instantclient-*.zip`, `libaio`, `ldconfig`, `LD_LIBRARY_PATH` / `ORACLE_HOME` / `TNS_ADMIN` env, `COPY instantclient...`. The image needs none of it.
- [ ] Remove DB credentials (`DB_HOST`…`DB_PASSWORD`) from `.env`, compose files, CI secrets and deployment config; add `DB_API_URL` + `DB_ACCESS_KEY`.
- [ ] Remove `oracledb` / `cx_Oracle` / ORM + driver imports and pool/connection modules from the app; replace each SQL call with a saved-query call (`query` / `execute`).
- [ ] Remove `tnsnames.ora` / wallet files if present.
- [ ] Network: the app container/host only needs to reach the DB Access API host and the LLM hosts — close any direct route to the Oracle listener (port 1521).
- [ ] Verify: `grep -ri "oracledb\|instantclient\|ORACLE_" .` over the app returns nothing (outside the installed `aicore` package).

> `aicore` itself currently lists `oracledb` as a dependency, so `pip` still installs it transitively. That's a package detail, not permission to use it: **app code never imports it**, and it never connects anywhere, so no Instant Client is needed.

### 8.4 Custom queries (your own SQL, through the API)

`custom_query` / `custom_execute` send **your SQL text** to the DB Access API,
which runs it on the server. Still no direct DB connection — but it is the
sharpest tool, so it is **opt-in per API key** and the exception, not the default.

| function | what it runs | key needs | returns |
|---|---|---|---|
| `custom_query(sql, params)` | one `SELECT` / `WITH … SELECT` | **Custom read** | `ApiResult` |
| `custom_execute(sql, params)` | one `INSERT` / `UPDATE … WHERE` | **Custom write** + INSERT/UPDATE permission | rows affected (`int`, committed) |

**Rules**
1. **Saved query first.** Use custom SQL only for genuinely ad-hoc/one-off needs (exploration, admin scripts, rarely-used lookups). Anything an app does repeatedly belongs in a named saved query (§8.2): it can be reviewed, tuned and permissioned once.
2. **Always `:binds`. Never format values into the SQL string** (no f-strings, `%`, `+`, `.format`). The SQL text must be a constant in your code; only the params dict varies.
   ```python
   # RIGHT
   custom_query("select id, name from myapp.records where id = :m", {"m": mrno})
   # WRONG — SQL injection
   custom_query(f"select * from myapp.records where id = '{mrno}'")
   ```
3. **One statement per call.** `custom_query` = a single SELECT/WITH. `custom_execute` = a single INSERT or UPDATE with a `WHERE`. **DELETE, MERGE, DDL (CREATE/ALTER/DROP/TRUNCATE) and PL/SQL blocks are refused by the API** — don't try to work around it; ask the admin for a saved query if you need those.
4. **Select only what you need** — name columns and filter by key; no `select *` on big tables, no unbounded scans. The server caps rows: check `result.has_more` and narrow the filter if it's `True`.
5. **Writes:** `custom_execute` returns rows affected. `0` is not an error (the `WHERE` matched nothing) — check it and treat it as "not found" if that matters. Writes are **never auto-retried** on timeout/network error (they may have committed): verify with a read before retrying yourself.
6. Values are sent as JSON (typed) — pass real `int` / `str` / `date` / `None` (NULL), not pre-formatted strings.
7. **Never pass user-supplied SQL** (e.g. an endpoint that takes a `sql` field) — custom SQL is for developers, not end users. Table/column names can't be bound; if they must vary, whitelist them from a fixed dict in code.
8. Errors are `DbApiError` as usual (`forbidden` = your key lacks Custom read/write; `invalid_request` = rejected statement / bad binds; `query_timeout` = too heavy, narrow or ask for a saved query). Log `request_id`.
9. Don't put PHI-heavy result sets into `@ai_history` input/output — store ids/counts (§9).

```python
from aicore import custom_query, custom_execute, DbApiError

try:
    res = custom_query(
        "select id, name from myapp.records where org_id = :o and status = :s",
        {"o": 5, "s": "ACTIVE"})
    patients = res.dicts()                      # list[dict]
    if res.has_more:
        ...                                     # result was cut by the server row limit

    n = custom_execute(
        "update myapp.notes set note = :n where id = :i",
        {"n": "Vitals stable", "i": 42})
    if n == 0:
        ...                                     # nothing matched
except DbApiError as e:
    log.error("db api %s %s req=%s", e.status, e.code, e.request_id)
    raise
```

### 8.5 Running it simply (smoke test in 1 minute)

1. Install: `pip install git+<repo-url>` (or `pip install -e .` in this repo).
2. Put the two settings in `.env` at the project root (or export them):
   ```env
   DB_API_URL=http://<api-host>:<port>
   DB_ACCESS_KEY=<your-access-key>
   APP_ID=13
   ```
3. Run from a terminal, no app needed:
   ```bash
   # saved query by name
   python -c "from aicore import query; print(query('patient_by_mrno', mrno='1234567').dicts())"

   # custom SELECT (only if your key has Custom read)
   python -c "from aicore import custom_query; print(custom_query('select 1 as ok from dual').dicts())"

   # DB reachable through the API? (returns latency in ms)
   python -c "from aicore.dbapi import check_db; print(check_db(), 'ms')"

   # model/key config resolves? (uses the same API)
   python -m aicore GENERATE_MDT
   ```
   Expected: `[{'OK': 1}]` for the custom check (column names come back as the server returns them — usually upper-case on Oracle).
4. Reading the failure:
   | you see | meaning / fix |
   |---|---|
   | `DbConfigError: Missing DB API env vars` | `DB_API_URL` / `DB_ACCESS_KEY` not set or `.env` not found (set `LIB_AI_ENV_FILE` to the file path) |
   | `DbApiError 0 network_error` | app host can't reach the API host/port |
   | `DbApiError 401/403 … forbidden` | wrong key, or key lacks Custom read/write or INSERT/UPDATE |
   | `DbApiError … invalid_request` | unknown query name, missing/extra bind, or statement type refused |
   | `DbApiError … query_timeout` | too heavy — narrow the filter or request a saved query |
   Always quote the `request id` from the error when asking the admin for help.

---

## 9. Security and data handling

- Never log, return, or store `cfg["api_key"]`. Log `cfg["api_key_meta"]` (already redacted) if you need to identify the key used.
- Don't put PHI beyond what the audit needs into `input`/`output`; prefer references (ids) over raw patient text where the full content already lives elsewhere. History is shared audit data, retained long term.
- `DB_ACCESS_KEY` is a secret: keep in `.env`/secret store, never commit, never log. DB access rules: §8.
- `get_model_config` errors (`ModelLookupError`, `AppNotRegisteredError`, `AmbiguousModelError`) are platform data problems — map them to **503**, not 500.

---

## 10. Complete example (two steps: Gemini → Ollama)

```python
import time
from fastapi import FastAPI, Request
from pydantic import BaseModel
from aicore import add_health_endpoint, ai_history, get_model_config
from pricing import step_cost, PRICING_AS_OF       # your app's pricing table

app = FastAPI()                       # APP_ID=13 in .env
add_health_endpoint(app)              # probes the URLs this app uses

class Req(BaseModel):
    note: str
    mrno: str; requested_by: str; org_id: int; zon_id: int; loc_id: int

@app.post("/summarize")
@ai_history(task_type="SUMMARIZE_NOTE")
async def summarize(req: Req, request: Request):
    in_steps, out_steps = [], []

    def run(n, name, purpose, prompt):
        cfg = get_model_config(purpose=purpose)              # per step, per request
        t0 = time.monotonic()
        text, usage = call_llm(cfg, prompt)                     # your wrapper → (text, usage dict)
        in_steps.append({"step": n, "name": name, "type": "llm",
            "provider": cfg["provider"], "model": cfg["model_name"], "purpose": purpose,
            "params": {"temperature": cfg["temperature"]}, "input": prompt[:2000]})
        out_steps.append({"step": n, "name": name, "type": "llm", "status": "ok",
            "provider": cfg["provider"], "model": cfg["model_name"], "output": text,
            "usage": usage, "cost": step_cost(cfg["provider"], cfg["model_name"], usage),
            "latency_ms": int((time.monotonic() - t0) * 1000), "error": None})
        return text

    entities = run(1, "extract_entities", "EXTRACT_ENTITIES", f"Extract entities:\n{req.note}")
    summary  = run(2, "summarize",        "SUMMARIZE",        f"Summarize using:\n{entities}")

    amounts = [s["cost"]["amount"] for s in out_steps]
    by_prov = {}
    for s in out_steps:
        by_prov[s["provider"]] = by_prov.get(s["provider"], 0.0) + (s["cost"]["amount"] or 0.0)
    return {
        "input":  {"request": {"note_chars": len(req.note), "mrno": req.mrno}, "steps": in_steps},
        "output": {"cost": {"currency": "USD",
                            "total": sum(a for a in amounts if a is not None),
                            "by_provider": by_prov,
                            "total_tokens": sum(s["usage"]["total_tokens"] for s in out_steps),
                            "incomplete": any(a is None for a in amounts),
                            "pricing_as_of": PRICING_AS_OF},
                   "steps": out_steps},
        "summary": summary,                    # client-facing extra key
    }
```

---

## 11. Error reference

| Exception (from `aicore`) | when | HTTP (FastAPI) | action |
|---|---|---|---|
| `DbConfigError` | `DB_API_URL` / `DB_ACCESS_KEY` missing | — | fix `.env` |
| `DbApiError` | any DB Access API failure (`.status .code .message .request_id`) | 500 unless mapped | branch on `.code`; log `.request_id` |
| `ModelLookupError` | no row for (app_id, purpose) | map to 503 | register in AICORE |
| `AppNotRegisteredError` | app_id not registered | map to 503 | contact platform team |
| `AmbiguousModelError` | >1 config rows (data problem) | map to 503 | contact platform team |
| `AIHistoryContextError` | 5 audit fields missing | **automatic 400** | client must send them |
| `AiHistoryFormatError` | handler broke return contract | **automatic 400** | fix the endpoint |

Common mistakes:

| Symptom | Cause | Fix |
|---|---|---|
| `ValueError: task_type is REQUIRED` at import | empty/missing `task_type` | add a meaningful `task_type` |
| 400 `missing: mr_no, loc_id` | client doesn't send audit fields | add them to Form/body/headers |
| 400 `@ai_history contract violated` | endpoint returns old shape | wrap the response under `{"input":…, "output":…}` (extra keys allowed) |
| route function named `model_config` misbehaves | FastAPI reserved name | rename the function |
| `/health` green but app broken | the model isn't registered (active) for this `APP_ID`, or has no URL, so nothing is probed | register the model/URL in the registry |
| `AppIdMissingError` | `APP_ID` not in `.env` / environment | add `APP_ID=<id>` |

---

## 12. Migrating an EXISTING codebase

1. `pip install` aicore; add `DB_API_URL` + `DB_ACCESS_KEY` to `.env`; run `python -m aicore <id> <purpose>`.
2. **Replace hand-rolled model config / hardcoded keys** (`MODEL=…`, `API_KEY=…`, `URL=…`, `TEMP=…`) with `get_model_config(purpose=...)`; delete the old constants/env keys. Put `APP_ID=<id>` in `.env` — the single source of the app id.
3. **Wrap each AI endpoint:** route → `@ai_history(task_type)` → handler; make the 5 audit fields reach it (Form, JSON, query, or `x-mr-no`/`x-user`/`x-org`/`x-zon`/`x-loc` headers); change the return to the step-wise `{"input", "output"}` (§6) with cost (§7).
4. **Wire `/health`** (§4).
5. **Remove direct DB access** — follow the cleanup list in §8.3 (Instant Client dir, Dockerfile steps, DB credentials, driver imports).
6. Walk the checklist in §13.

Before/after of an endpoint:
```python
# BEFORE
@app.post("/transcribe")
async def transcribe(note: UploadFile, requested_by: str = Form(...)):
    return {"summary": call_llm(note)}

# AFTER
@app.post("/transcribe")
@ai_history(task_type="TRANSCRIBE")
async def transcribe(note: UploadFile, mrno: str = Form(...), requested_by: str = Form(...),
                     org_id: int = Form(...), zon_id: int = Form(...), loc_id: int = Form(...)):
    ...                      # steps + cost as in §10
    return {"input": {...}, "output": {...}, "summary": text}
```

---

## 13. Review checklist (for whoever approves the PR)

1. `/health` present (no arguments needed beyond `APP_ID` in `.env`); 503 on dead host.
2. No hardcoded model/key/URL; `get_model_config` per request; purposes registered in AICORE.
3. Every AI endpoint: route → `@ai_history` → handler; non-empty `task_type`.
4. Every `@ai_history` endpoint returns `input.request`, `input.steps[]`, `output.cost`, `output.steps[]`; step numbers match 1:1.
5. Gemini steps have `usage` + numeric `cost.amount`; Ollama steps `amount: 0.0`; `cost.total` equals the sum.
6. Multi-call endpoints: retries/fallbacks appear as their own steps.
7. Payloads fit the 15,000-char cap (references/previews for large content); cost sorts before steps.
8. **No direct DB access:** no `oracledb`/driver imports, no `DB_HOST…DB_PASSWORD`, no Instant Client dir, no Oracle steps in the Dockerfile; data via `query()/execute()` only.
9. No secrets in payloads or logs; pricing table has `pricing_as_of` and every model in use.
