"""DB Access API client — the public ``query`` / ``execute`` for aicore apps.

Talks to the *DB Access API* over HTTP instead of opening Oracle
connections. The API runs **saved, named queries** (an admin defines them on
the dashboard); you pass the query *name* plus its *parameters*:

    from aicore import query, execute, custom_query, custom_execute

    rows = query("patient_by_mrno", {"mrno": "1234567"}).dicts()
    rows = query("patient_by_mrno", mrno="1234567").dicts()      # kwargs also work
    n    = execute("add_note", {"mrno": "1234567", "note": "Vitals stable"})

    # only if an admin enabled "Custom read"/"Custom write" on your API key:
    rows = custom_query("select id from myapp.records where id = :m", {"m": "1"})
    n    = custom_execute("update myapp.notes set note = :n where id = :i",
                          {"n": "done", "i": 42})

Configuration (environment / .env):
    DB_API_URL      base URL, e.g. http://<api-host>:<port>        (required)
    DB_ACCESS_KEY   the per-application key ``<your-access-key>``           (required)
    DB_API_TIMEOUT  client timeout in seconds                     (default 200)
    DB_API_USE_PROXY  1 = honour HTTP(S)_PROXY env vars (default 0: bypass) (optional)
    DB_API_RETRIES  retries on 429/503 (and network errors, reads only)
                    with 1,2,4,8 s backoff                        (default 4)

Or in code: ``aicore.dbapi.configure(base_url=..., api_key=..., ...)``.

Errors: every failure raises ``DbApiError`` (``.status``, ``.code``,
``.message``, ``.request_id``). Branch on ``.code`` (``forbidden``,
``invalid_request``, ``query_timeout`` ...). Quote ``.request_id`` to the admin.

Write safety: ``execute`` / ``custom_execute`` are NOT retried after a network
error or timeout (the write may have committed). They ARE retried when the
server answered ``429`` / ``503`` — those are rolled back, nothing changed.
"""
from __future__ import annotations

import datetime as _dt
import decimal as _decimal
import logging
import os
import threading
import time
from typing import Any, Optional

from .db import DbConfigError, QueryResult

__all__ = [
    "query", "execute", "custom_query", "custom_execute", "configure", "check_db",
    "DbApiError", "ApiResult",
]

log = logging.getLogger("aicore.dbapi")

_RETRYABLE = {"rate_limited", "db_busy", "db_unavailable"}
_API = "/api/v1/db"


class DbApiError(RuntimeError):
    """Any failed call to the DB Access API."""

    def __init__(self, status: int, code: str, message: str,
                 request_id: Optional[str] = None):
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        rid = f" (request id {request_id})" if request_id else ""
        super().__init__(f"{status} {code}: {message}{rid}")


class ApiResult(QueryResult):
    """``QueryResult`` plus ``.has_more`` (server row limit cut the result)
    and ``.request_id``. Use ``.dicts()`` for the rows as ``{column: value}``."""
    __slots__ = ("has_more", "request_id")

    def __init__(self, columns, rows, has_more=False, request_id=None):
        super().__init__(columns, rows)
        self.has_more = has_more
        self.request_id = request_id

    def __repr__(self):
        more = ", has_more" if self.has_more else ""
        return f"ApiResult(columns={self.columns}, {len(self.rows)} row(s){more})"


# --------------------------------------------------------------------------- #
# configuration + shared HTTP session
# --------------------------------------------------------------------------- #

_cfg: dict = {}
_session = None
_lock = threading.Lock()


def configure(base_url: Optional[str] = None, api_key: Optional[str] = None,
              timeout: Optional[float] = None, retries: Optional[int] = None) -> None:
    """Override env-based settings (any argument left ``None`` is unchanged).
    Resets the HTTP session so a new key/URL takes effect."""
    global _session
    with _lock:
        for k, v in (("base_url", base_url), ("api_key", api_key),
                     ("timeout", timeout), ("retries", retries)):
            if v is not None:
                _cfg[k] = v
        if _session is not None:
            _session.close()
        _session = None


def _settings() -> dict:
    base = _cfg.get("base_url") or os.getenv("DB_API_URL")
    key = _cfg.get("api_key") or os.getenv("DB_ACCESS_KEY")
    missing = [n for n, v in (("DB_API_URL", base), ("DB_ACCESS_KEY", key)) if not v]
    if missing:
        raise DbConfigError("Missing DB API env vars: " + ", ".join(missing))
    return {
        "base_url": base.rstrip("/"),
        "api_key": key,
        "timeout": float(_cfg.get("timeout") or os.getenv("DB_API_TIMEOUT") or 200),
        "retries": int(_cfg.get("retries") if _cfg.get("retries") is not None
                       else os.getenv("DB_API_RETRIES", "4")),
    }


def _get_session(api_key: str):
    global _session
    with _lock:
        if _session is None:
            import requests  # deferred: only needed when talking to the API
            _session = requests.Session()          # keep-alive across calls
            _session.headers["X-API-Key"] = api_key
            # The API is an internal service: ignore HTTP(S)_PROXY from the
            # environment unless explicitly enabled (DB_API_USE_PROXY=1).
            _session.trust_env = os.getenv("DB_API_USE_PROXY", "0") == "1"
        return _session


# --------------------------------------------------------------------------- #
# value normalisation
# --------------------------------------------------------------------------- #

def _jsonable(v: Any, name: str) -> Any:
    """Body value → string | finite number | bool | None (API accepts no more)."""
    if v is None or isinstance(v, (str, bool, int, float)):
        return v
    if isinstance(v, _decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, _dt.datetime) else v.isoformat()
    raise TypeError(f"parameter {name!r}: unsupported type {type(v).__name__} "
                    "(allowed: str, number, bool, None, date/datetime, Decimal)")


def _url_value(v: Any, name: str) -> str:
    """Query-string value → text ('' means NULL; bool → '1'/'0')."""
    v = _jsonable(v, name)
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def _merge(params: Optional[dict], kwargs: dict) -> dict:
    merged = dict(params or {})
    dup = set(merged) & set(kwargs)
    if dup:
        raise ValueError(f"parameter(s) given twice (dict and keyword): {sorted(dup)}")
    merged.update(kwargs)
    if any(str(k).lower() == "table_name" for k in merged):
        raise ValueError("'table_name' is reserved for the query name — "
                         "rename that bind parameter")
    return merged


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #

def _request(method: str, path: str, *, params=None, json=None,
             retry_network: bool) -> Any:
    import requests
    cfg = _settings()
    session = _get_session(cfg["api_key"])
    url = cfg["base_url"] + path
    attempt = 0
    while True:
        try:
            resp = session.request(method, url, params=params, json=json,
                                   timeout=cfg["timeout"])
        except requests.RequestException as exc:
            if retry_network and attempt < cfg["retries"]:
                log.warning("DB API network error (%s); retry %d", exc, attempt + 1)
                time.sleep(2 ** attempt)
                attempt += 1
                continue
            raise DbApiError(0, "network_error", str(exc)) from exc

        rid = resp.headers.get("X-Request-ID")
        if resp.ok:
            try:
                body = resp.json()
            except ValueError as exc:
                raise DbApiError(resp.status_code, "bad_response",
                                 "API returned non-JSON on success", rid) from exc
            body["_request_id"] = rid
            return body

        try:
            err = resp.json()["error"]
            code, msg = str(err.get("code", "error")), str(err.get("message", ""))
        except (ValueError, KeyError, TypeError, AttributeError):
            code, msg = "http_error", (resp.text or resp.reason or "")[:300]
        if code in _RETRYABLE and attempt < cfg["retries"]:
            log.warning("DB API %s (%s); retry %d", code, rid, attempt + 1)
            time.sleep(2 ** attempt)
            attempt += 1
            continue
        raise DbApiError(resp.status_code, code, msg, rid)


def _to_result(body: dict) -> ApiResult:
    items = body.get("items") or []
    cols = list(items[0].keys()) if items else []
    rows = [[it.get(c) for c in cols] for it in items]
    res = ApiResult(cols, rows, bool(body.get("hasMore")), body.get("_request_id"))
    if res.has_more:
        log.warning("DB API result truncated by the server row limit "
                    "(request id %s) — narrow the filter", res.request_id)
    return res


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def query(name: str, /, params: Optional[dict] = None, **kwargs) -> ApiResult:
    """Run a saved READ query by name → ``ApiResult`` (rows, no commit).

        query("patient_by_mrno", {"mrno": "1234567"}).dicts()
        query("patient_by_mrno", mrno="1234567").first

    Every bind of the saved SQL must be supplied; unknown names are rejected.
    ``None`` binds NULL. Values travel in the URL, so they are sent as text.
    """
    values = _merge(params, kwargs)
    qs = {"table_name": name, **{k: _url_value(v, k) for k, v in values.items()}}
    body = _request("GET", f"{_API}/query", params=qs, retry_network=True)
    return _to_result(body)


def execute(name: str, /, params: Optional[dict] = None, **kwargs) -> int:
    """Run a saved WRITE query (INSERT/UPDATE) by name → ``rowsAffected``.

        execute("add_note", {"mrno": "1234567", "note": "Vitals stable"})

    Committed on success, rolled back on any error. Values go in the JSON body.
    ``0`` rows affected is not an error (the WHERE matched nothing) — check it.
    Needs INSERT/UPDATE permission on the API key.
    """
    values = {k: _jsonable(v, k) for k, v in _merge(params, kwargs).items()}
    body = _request("POST", f"{_API}/execute", params={"table_name": name},
                    json=values, retry_network=False)
    return body["rowsAffected"]


def custom_query(sql: str, params: Optional[dict] = None) -> ApiResult:
    """Run your own single SELECT / WITH…SELECT (key needs *Custom read*).

        custom_query("select * from myapp.records where id = :m", {"m": "1"})

    Always use ``:binds`` for values — never format them into ``sql``.
    """
    values = {k: _jsonable(v, k) for k, v in (params or {}).items()}
    body = _request("POST", f"{_API}/sql/query",
                    json={"sql": sql, "params": values}, retry_network=True)
    return _to_result(body)


def custom_execute(sql: str, params: Optional[dict] = None) -> int:
    """Run your own single INSERT / UPDATE…WHERE → ``rowsAffected``
    (key needs *Custom write* + INSERT/UPDATE). DELETE/MERGE/DDL/PL-SQL refused."""
    values = {k: _jsonable(v, k) for k, v in (params or {}).items()}
    body = _request("POST", f"{_API}/sql/execute",
                    json={"sql": sql, "params": values}, retry_network=False)
    return body["rowsAffected"]


def check_db() -> float:
    """Ping ``GET /health/db`` (no key needed by the API, but the client sends it).
    Returns the DB latency in ms; raises ``DbApiError`` if the DB is unreachable."""
    body = _request("GET", f"{_API}/health/db", retry_network=True)
    return float((body.get("checks", {}).get("db") or {}).get("ms") or 0.0)
