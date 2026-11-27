"""AI request tracking: the ``@ai_history`` decorator.

Writes a life-cycle row per tracked endpoint call to
`the audit-history table` (via the DB Access API saved queries):

    INSERT STATUS='PROCESSING'  →  handler runs  →
    UPDATE STATUS='COMPLETED' + INPUT_JSON/OUTPUT_JSON   (your "input"/"output")
    UPDATE STATUS='ERROR'       (handler raised, OR contract broken,
                                 OR mandatory request fields missing)

Guarantees:
    * Tracking-DB failures are logged, NEVER raised — a broken history log
      cannot take a valid endpoint down.
    * The 5 mandatory request fields (``mrno, requested_by, org_id,
      zon_id, loc_id``) are ENFORCED, no opt-out; breach →
      ``AIHistoryContextError`` → HTTP 400 (FastAPI/starlette) with the
      exact missing fields listed.
    * The handler return contract (``{"input": ..., "output": ...}``) is
      VALIDATED; breach → ``AiHistoryFormatError`` → HTTP 400 (FastAPI/starlette).
    * ``TRACKING_ENABLED=false`` → decorator becomes a no-op passthrough.
"""
from __future__ import annotations

import asyncio
import dataclasses as _dc
import functools
import inspect as _insp
import uuid
from typing import Any, Callable, Optional, Tuple

from .dbapi import execute as _api_execute, query as _api_query
from .env import get_app_id
from .serialize import (
    dumps_truncated, is_request, mask_secrets, request_meta, to_jsonable,
)

__all__ = [
    "ai_history",
    "AiHistoryFormatError", "AIHistoryContextError", "AI_HISTORY_REQUIRED_FIELDS",
]


# --------------------------------------------------------------------------- #
# history-table writers
# --------------------------------------------------------------------------- #

# Saved queries on the DB Access API (the SQL lives in the dashboard):
#   ai_history_start            INSERT  — PROCESSING row
#   ai_history_find_id          SELECT  — PROCESSING_ID by task_id (replaces CURRVAL:
#                                         each API call is its own DB session)
#   ai_history_finish           UPDATE  — status + output
#   ai_history_finish_with_input UPDATE — status + input + output
_STATUS_START = "PROCESSING"
_STATUS_OK = "COMPLETED"
_STATUS_ERR = "ERROR"


def _hist_start_request(*, app_id, input_json, task_type=None, task_id=None,
                        mr_no=None, requested_by=None,
                        org_id=None, zon_id=None, loc_id=None,
                        priority=0) -> int:
    """INSERT a PROCESSING row, return the new PROCESSING_ID."""
    task_id = task_id or str(uuid.uuid4())      # the lookup key — must be set
    _api_execute("ai_history_start", {
        "app_id": app_id, "priority": priority,
        "task_id": task_id, "task_type": task_type, "mr_no": mr_no,
        "input_json": input_json, "requested_by": requested_by,
        "org_id": org_id, "zon_id": zon_id, "loc_id": loc_id,
        "status": _STATUS_START,
    })
    return int(_api_query("ai_history_find_id", {"task_id": task_id}).first[0])


def _hist_finish_request(processing_id: int, status: str,
                         output_json: Optional[str] = None,
                         input_json: Optional[str] = None) -> None:
    """UPDATE the row: STATUS + OUTPUT_JSON (+ INPUT_JSON if given) + AI_RESPONSE_DATE."""
    if input_json is None:
        _api_execute("ai_history_finish", {
            "status": status, "output_json": output_json, "pid": processing_id})
    else:
        _api_execute("ai_history_finish_with_input", {
            "status": status, "input_json": input_json,
            "output_json": output_json, "pid": processing_id})


# --------------------------------------------------------------------------- #
# request field picking (mandatory-context enforcement)
# --------------------------------------------------------------------------- #

#: fields a request MUST carry when its endpoint uses @ai_history
AI_HISTORY_REQUIRED_FIELDS = ("mr_no", "requested_by", "org_id", "zon_id", "loc_id")

_FIELD_SYNONYMS = {
    "mr_no":      ("mrno", "mr_no", "mr_number", "mrnumber", "x-mr-no"),
    "requested_by": ("requested_by", "requestedby", "username", "x-user", "user"),
    "org_id":     ("org_id", "orgid", "organization_id", "x-org"),
    "zon_id":     ("zon_id", "zonid", "zone_id", "zid", "x-zon"),
    "loc_id":     ("loc_id", "locid", "location_id", "x-loc"),
    "task_id":    ("task_id", "taskid", "x-task-id"),
}


def _walk(obj: Any, dotted: str) -> Any:
    cur = obj
    for part in dotted.split("."):
        if cur is None:
            return None
        cur = cur.get(part) if isinstance(cur, dict) else getattr(cur, part, None)
    return cur


def _pick_str(args_map: dict, name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    v = _walk(args_map, name)
    if v is not None:
        return str(v)
    for arg in args_map.values():
        if isinstance(arg, dict):
            v = _walk(arg, name)
            if v is not None:
                return str(v)
    return None


def _find_in_scope(scope: Any, synonyms: tuple) -> Optional[str]:
    if not isinstance(scope, dict):
        return None
    low = {str(k).lower(): v for k, v in scope.items() if v is not None}
    for s in synonyms:
        if s in low:
            return str(low[s])
    return None


def _auto_pick(args_map: dict, reqmeta: dict, field: str) -> Optional[str]:
    synonyms = _FIELD_SYNONYMS.get(field)
    if not synonyms:
        return None
    v = _find_in_scope(args_map, synonyms)
    if v is not None:
        return v
    for arg in args_map.values():
        if isinstance(arg, dict):
            v = _find_in_scope(arg, synonyms)
            if v is not None:
                return v
    v = _find_in_scope(reqmeta.get("query"), synonyms)
    if v is not None:
        return v
    return _find_in_scope(reqmeta.get("headers"), synonyms)


def _args_map(fn: Callable, args: tuple, kwargs: dict) -> dict:
    """Map every handler argument (except Request) to JSON-able values."""
    out: dict = {}
    try:
        sig = _insp.signature(fn)
    except (TypeError, ValueError):
        sig = None

    if sig is not None:
        pos_names = [n for n, p in sig.parameters.items()
                     if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        for i, name in enumerate(pos_names):
            if name in kwargs:
                v = kwargs[name]
            elif i < len(args):
                v = args[i]
            else:
                continue
            if is_request(v):
                continue
            out[name] = to_jsonable(v)
        for i, a in enumerate(args[len(pos_names):]):
            if not is_request(a):
                out[f"arg{i}"] = to_jsonable(a)
        for k, v in kwargs.items():
            if k not in out and not is_request(v):
                out[k] = to_jsonable(v)
    else:
        for i, a in enumerate(args):
            if not is_request(a):
                out[f"arg{i}"] = to_jsonable(a)
        for k, v in kwargs.items():
            if not is_request(v):
                out[k] = to_jsonable(v)
    return out


def _request_from(args: tuple, kwargs: dict) -> Any:
    return next((v for v in list(args) + list(kwargs.values())
                 if is_request(v)), None)


def _form_fields(request: Any) -> Any:
    if request is None or not hasattr(request, "form"):
        return {}
    try:
        ct = (request.headers.get("content-type") or "").lower()
        if "form-data" not in ct and "urlencoded" not in ct:
            return {}
        res = request.form()
    except Exception:
        return {}
    if _insp.isawaitable(res):
        return res
    return _form_dict(res)


def _form_dict(form: Any) -> Any:
    try:
        return {str(k): v for k, v in form.items()}
    except Exception:
        return form


# --------------------------------------------------------------------------- #
# errors (soft subclass of starlette.HTTPException → automatic HTTP 400)
# --------------------------------------------------------------------------- #

def _contract_message(got: Any) -> str:
    preview = repr(got)
    if len(preview) > 300:
        preview = preview[:300] + " ..."
    return (
        "@ai_history contract violated.\n"
        "The decorated endpoint must return a dict with 'input' and\n"
        "'output' keys (values can be anything JSON-serializable; lists\n"
        "allowed for multiple LLM calls):\n"
        '    {"input": ...what you logged as input...,\n'
        '     "output": ...what you logged as output...}\n'
        f"Got: {type(got).__name__}: {preview}\n"
        "Wrap what you want history to keep in {{'input': ..., 'output': ...}}\n"
        "before returning (extra keys are allowed)."
    )


def _context_message(missing) -> str:
    return (
        "@ai_history: required request fields are MISSING — call rejected.\n"
        f"    missing: {', '.join(missing)}\n"
        "Provide them with the request — any of: Form / multipart fields,\n"
        "JSON body, query params, or headers (x-mr-no / x-user / x-org / x-zon / x-loc).\n"
        "Accepted names: mrno, requested_by, org_id, zon_id, loc_id (synonyms OK).\n"
    )


try:
    # Soft dependency: FastAPI/starlette apps get an automatic
    # HTTP 400 {"detail": <clear message>} — NO extra app code needed.
    from starlette.exceptions import HTTPException as _HTTPException

    class AiHistoryFormatError(ValueError, _HTTPException):
        """@ai_history contract violation: the handler did not return
        {'input': ..., 'output': ...}."""

        def __init__(self, got: Any):
            msg = _contract_message(got)
            ValueError.__init__(self, msg)
            _HTTPException.__init__(self, status_code=400, detail=msg)

    class AIHistoryContextError(ValueError, _HTTPException):
        """@ai_history required fields are missing from the request."""

        def __init__(self, missing):
            msg = _context_message(missing)
            ValueError.__init__(self, msg)
            _HTTPException.__init__(self, status_code=400, detail=msg)

except ImportError:
    # non-web context (scripts, workers): plain ValueErrors, same messages
    class AiHistoryFormatError(ValueError):
        """@ai_history contract violation."""

        def __init__(self, got: Any):
            ValueError.__init__(self, _contract_message(got))

    class AIHistoryContextError(ValueError):
        """@ai_history required fields missing."""

        def __init__(self, missing):
            ValueError.__init__(self, _context_message(missing))


def _split_contract(result: Any) -> dict:
    """Validate the @ai_history return contract; return {'input','output'}."""
    d = result
    if not isinstance(d, dict):
        if hasattr(d, "model_dump"):                       # pydantic v2
            d = d.model_dump()
        elif _dc.is_dataclass(d) and not isinstance(d, type):
            d = _dc.asdict(d)
    if not isinstance(d, dict) or "input" not in d or "output" not in d:
        raise AiHistoryFormatError(result)
    return d


# --------------------------------------------------------------------------- #
# the tracking record
# --------------------------------------------------------------------------- #

class _Record:
    """INSERT ... PROCESSING  →  UPDATE ... COMPLETED | ERROR.

    Every DB failure is swallowed+logged — tracking must never break the
    endpoint being tracked.
    """

    def __init__(self, cfg: dict, args_map: dict, request: Any):
        import logging
        self._log = logging.getLogger("aicore.history")
        self.pid: Optional[int] = None
        self.task_id: Optional[str] = None
        self._reqmeta = request_meta(request)
        self._input = dumps_truncated({"args": args_map, "request": self._reqmeta})
        try:
            cols = self._cols(cfg, args_map, self._reqmeta)
            self.task_id = cols.get("task_id") or str(uuid.uuid4())
            self.pid = _hist_start_request(
                app_id=cfg["app_id"],
                input_json=self._input,
                task_type=cfg.get("task_type"),
                task_id=self.task_id,
                mr_no=cols.get("mr_no"),
                requested_by=cols.get("requested_by"),
                org_id=cols.get("org_id"),
                zon_id=cols.get("zon_id"),
                loc_id=cols.get("loc_id"),
                priority=cfg.get("priority", 0),
            )
        except Exception:
            self._log.exception("ai_history: start_request failed (app_id=%s)",
                                cfg.get("app_id"))

    # detect a metadata field: explicit mapping → handler args → request meta
    @staticmethod
    def _cols(cfg: dict, args_map: dict, reqmeta: dict) -> dict:
        def col(field: str) -> Optional[str]:
            return (_pick_str(args_map, cfg.get(field))
                    or _auto_pick(args_map, reqmeta, field))
        return {f: col(f) for f in ("task_id", "mr_no", "requested_by",
                                    "org_id", "zon_id", "loc_id")}

    def finish(self, success: bool, output: Any = None,
               input_payload: Any = None,
               exc: Optional[BaseException] = None) -> None:
        if self.pid is None:
            return
        if success:
            try:
                _hist_finish_request(
                    self.pid, _STATUS_OK,
                    dumps_truncated(output),
                    dumps_truncated(input_payload) if input_payload is not None else None,
                )
            except Exception:
                self._log.exception("ai_history: record_ok failed for %s", self.pid)
        else:
            payload = {"error": str(exc), "exception": type(exc).__name__}
            try:
                _hist_finish_request(self.pid, _STATUS_ERR, dumps_truncated(payload))
            except Exception:
                self._log.exception("ai_history: record_error failed for %s", self.pid)


# --------------------------------------------------------------------------- #
# the decorator
# --------------------------------------------------------------------------- #

def _tracking_enabled() -> bool:
    import os
    return os.getenv("TRACKING_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on")


def _make_wrapper(fn: Callable, cfg: dict):
    def _reject_missing(args_map: dict, reqmeta: dict) -> None:
        """Enforce the mandatory request fields; record+raise the breach."""
        cols = _Record._cols(cfg, args_map, reqmeta)
        missing = [f for f in AI_HISTORY_REQUIRED_FIELDS
                   if not (cols.get(f) or "").strip()]
        if missing:
            record = _Record(cfg, args_map, None)
            err = AIHistoryContextError(missing)
            record.finish(False, exc=err)   # ERROR row + clear message
            raise err

    def _setup(args: tuple, kwargs: dict):
        # NOTE: form may come back as an AWAITABLE (request.form() on a sync
        # handler) — each wrapper resolves it in its own event-loop context.
        req = _request_from(args, kwargs)
        args_map = _args_map(fn, args, kwargs)
        form = _form_fields(req)
        if form:
            args_map["_form"] = form
        return req, args_map
    if _insp.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*args, **kwargs):
            req, args_map = _setup(args, kwargs)
            form = args_map.get("_form")
            if _insp.isawaitable(form):
                args_map["_form"] = _form_dict(await form)
            _reject_missing(args_map, request_meta(req))
            record = _Record(cfg, args_map, req)
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                record.finish(False, exc=exc)
                raise
            try:
                payload = _split_contract(result)      # contract check
            except Exception as err:
                record.finish(False, exc=err)           # row: ERROR + message
                raise                                    # loud, clear failure
            record.finish(True, output=payload["output"],
                          input_payload=payload["input"])
            return result
        return awrapper

    @functools.wraps(fn)
    def swrapper(*args, **kwargs):
        req, args_map = _setup(args, kwargs)
        form = args_map.get("_form")
        if _insp.isawaitable(form):              # non-web async request object
            args_map["_form"] = _form_dict(asyncio.run(form))
        _reject_missing(args_map, request_meta(req))
        record = _Record(cfg, args_map, req)
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            record.finish(False, exc=exc)
            raise
        try:
            payload = _split_contract(result)          # contract check
        except Exception as err:
            record.finish(False, exc=err)               # row: ERROR + message
            raise                                        # loud, clear failure
        record.finish(True, output=payload["output"],
                      input_payload=payload["input"])
        return result
    return swrapper


def ai_history(
    *,
    app_id: Optional[int] = None,             # default: APP_ID from .env
    task_type: Optional[str] = None,          # REQUIRED (validated below)
    priority: int = 0,
    # optional column mappings (dotted path into handler args). If omitted,
    # each field is AUTO-DETECTED from the request (Form/body/query/headers).
    task_id: Optional[str] = None,
    mr_no: Optional[str] = None,
    requested_by: Optional[str] = None,
    org_id: Optional[str] = None,
    zon_id: Optional[str] = None,
    loc_id: Optional[str] = None,
) -> Callable:
    """Track an endpoint call in the audit-history table.

    Put it BETWEEN the route decorator and the function:

        @app.post("/process")
        @ai_history(task_type="BULKTRANSCRIPT")     # app id from .env APP_ID
        async def process(req: MyRequest, request: Request):
            return {"input": <you decide>, "output": <you decide>}

    MANDATORY (checked for you):
      * ``task_type`` — must be a non-empty string, else this decorator
        raises a ``ValueError`` at import time.
      * request fields: ``mr_no, requested_by, org_id, zon_id, loc_id`` must
        exist on the incoming request (Form / multipart, JSON body, query
        params or headers). Missing → ``AIHistoryContextError`` → HTTP 400
        listing exactly what's missing; the row is recorded as ERROR and
        the handler never runs. (No opt-out — these fields are mandatory
        by design.)

    CONTRACT: the handler must return a dict with ``'input'`` and
    ``'output'`` keys (values can be anything JSON-serializable; lists
    allowed). The decorator stores them as ``INPUT_JSON`` / ``OUTPUT_JSON``
    and returns the dict to the client. A return value that breaks the
    contract raises ``AiHistoryFormatError``.

    Optional explicit mappings (dotted paths into handler args) win over
    auto-detection: ``@ai_history(app_id=1, task_type="T", mr_no="req.mr")``.
    """
    if not str(task_type or "").strip():
        raise ValueError(
            "@ai_history: task_type is REQUIRED.\n"
            '    e.g.  @ai_history(task_type="MY_TASK")')
    app_id = get_app_id(app_id)       # explicit, else APP_ID from .env, else error

    cfg = dict(app_id=app_id, task_type=task_type, priority=priority,
               task_id=task_id, mr_no=mr_no, requested_by=requested_by,
               org_id=org_id, zon_id=zon_id, loc_id=loc_id)

    def wrap(fn: Callable) -> Callable:
        if not _tracking_enabled():
            return fn
        return _make_wrapper(fn, cfg)

    return wrap
