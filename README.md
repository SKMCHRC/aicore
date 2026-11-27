# class-ai

Shared Python package **`aicore`** for AICORE: model resolution, AI request
audit tracking, a shared health endpoint, and DB access through the DB Access API.

```
aicore/     ← THE package (import from `aicore`)
pyproject.toml · requirements.txt · README.md
```

`examples/`, `_archive/` and `AGENT.md` are **local-only** (git-ignored) — the repo ships the package.

## 1. Install (from GitHub)

```bash
pip install git+https://github.com/SKMCHRC/aicore.git            # latest main
pip install -U git+https://github.com/SKMCHRC/aicore.git         # upgrade
pip install "git+https://github.com/SKMCHRC/aicore.git@<tag-or-commit>"   # pin a version (recommended for prod)
pip install fastapi uvicorn                                      # your web framework, if not already
```

In your app's `requirements.txt`:
```
aicore @ git+https://github.com/SKMCHRC/aicore.git@<tag-or-commit>
```

Requires Python ≥ 3.9 and `git` on the machine. The repo is private, so `pip` needs
GitHub access: sign in once with `gh auth login` (or Git Credential Manager) and
`pip` will reuse it. **Never put a token in a committed file or URL.** In CI use a
deploy key or `GITHUB_TOKEN` via your secret store.

Check it installed: `python -c "import aicore; print(aicore.__version__)"`

## 2. Save the skill in YOUR project (do this first)

`aicore/SKILL.md` is the single source of truth for how to use this library
(rules, structures, examples). **Before writing any code that uses aicore, save
it into your project's skills folder** so Claude Code (or any AI agent) in that
project knows how to use aicore:

```bash
python -c "import aicore,shutil,pathlib; d=pathlib.Path('.claude/skills/aicore'); d.mkdir(parents=True,exist_ok=True); shutil.copy(pathlib.Path(aicore.__file__).with_name('SKILL.md'), d/'SKILL.md')"
```

Result: `<your-project>/.claude/skills/aicore/SKILL.md`. Commit it with your project.
**Re-run it after every upgrade** so the skill matches the installed version.

## 3. Configure

`.env` in your project root (never commit it):

```env
DB_API_URL=http://<api-host>:<port>     # DB Access API
DB_ACCESS_KEY=<your-access-key>                 # per-application key (secret)
APP_ID=999                              # YOUR registry app id (required)
```

That is all. No Oracle credentials, no Instant Client — apps never connect to a database directly.

## 4. Test run

**a) Command-line checks** (no app needed):

```bash
# model config + live key resolve for your app (uses the DB Access API)
python -m aicore <PURPOSE>                     # app id from APP_ID in .env, e.g. python -m aicore GENERATE_MDT

# DB Access API reachable? (prints latency in ms)
python -c "from aicore.dbapi import check_db; print(check_db(), 'ms')"

# a saved query by name
python -c "from aicore import query; print(query('patient_by_mrno', mrno='1234567').dicts())"
```

**b) Minimal app** — save as `main.py`:

```python
from fastapi import FastAPI, Request
from pydantic import BaseModel
from aicore import add_health_endpoint, ai_history

app = FastAPI()                                # APP_ID is read from .env
add_health_endpoint(app)                       # MANDATORY: GET /health

class Req(BaseModel):
    question: str      # the 5 audit fields arrive as headers (below)

@app.post("/ask")
@ai_history(task_type="ASK_TEST")
async def ask(req: Req, request: Request):
    # stub step — replace with get_model_config(purpose="<PURPOSE>") + a real LLM call
    return {
        "input":  {"request": {"q_chars": len(req.question)},
                   "steps": [{"step": 1, "name": "echo", "type": "tool", "input": req.question}]},
        "output": {"cost": {"currency": "USD", "total": 0.0},
                   "steps": [{"step": 1, "name": "echo", "type": "tool", "status": "ok",
                              "output": req.question.upper(),
                              "cost": {"amount": 0.0, "billable": False}, "latency_ms": 0}]},
        "answer": req.question.upper(),
    }
```

Run it and hit it:

```bash
uvicorn main:app --port 8000

# 1) health → 200 {"status":"healthy",...}  (503 if the LLM host is unreachable)
curl -i http://localhost:8000/health

# 2) missing audit fields → 400 listing exactly what is missing
curl -i -X POST http://localhost:8000/ask -H "content-type: application/json" -d '{"question":"hi"}'

# 3) with the 5 audit fields → 200 {"answer":"HI", ...}  and a PROCESSING→COMPLETED history row
curl -i -X POST http://localhost:8000/ask -H "content-type: application/json" \
  -H "x-mr-no: 1234567" -H "x-user: dr.smith" -H "x-org: 1" -H "x-zon: 1" -H "x-loc: 1" \
  -d '{"question":"hi"}'
```

Expected: `/health` 200, request 2 → **400**, request 3 → **200**. If `DB_API_URL` /
`DB_ACCESS_KEY` are not set the app still answers, but the history write fails and is
only logged (`ai_history: start_request failed`) — set them to see rows in
the audit-history table.

Same check without a server (pytest-style):

```python
from fastapi.testclient import TestClient      # pip install httpx
from main import app

c = TestClient(app)
assert c.get("/health").status_code in (200, 503)
assert c.post("/ask", json={"question": "hi"}).status_code == 400
h = {"x-mr-no": "1", "x-user": "u", "x-org": "1", "x-zon": "1", "x-loc": "1"}
assert c.post("/ask", json={"question": "hi"}, headers=h).json()["answer"] == "HI"
```

## 5. Using it in an app

```python
from aicore import (get_model_config, ai_history, add_health_endpoint,
                    query, execute, custom_query, custom_execute, DbApiError)

add_health_endpoint(app)                         # MANDATORY; probes the URLs this APP_ID uses

cfg = get_model_config(purpose="GENERATE_MDT")    # app id = APP_ID from .env
# cfg["model_name"], cfg["provider"], cfg["api_key"], cfg["url"], cfg["temperature"] ...

rows = query("patient_by_mrno", {"mrno": "1234567"}).dicts()                # saved query by NAME
n    = execute("add_note", {"mrno": "1234567", "note": "Vitals stable"})    # rows affected
# only if the admin enabled Custom read / Custom write on your key:
rows = custom_query("select * from myapp.records where id = :m", {"m": "1"}).dicts()
n    = custom_execute("update myapp.notes set note = :n where id = :i", {"n": "x", "i": 1})
# errors → DbApiError (.status .code .message .request_id)
```

**Full usage guide + mandatory app rules (health check, `@ai_history`, step-wise
input/output, cost per call, API-only DB access, migration, review checklist):
[`aicore/SKILL.md`](aicore/SKILL.md) — one file for humans and AI agents.**

Project conventions and change log live in the local `AGENT.md` (not part of the published repo).
