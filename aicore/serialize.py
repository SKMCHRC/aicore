"""Serialization, secret-masking and request-snapshot helpers.

Everything here is defensive: nothing in this module raises on exotic
inputs — worst case a value becomes ``str(v)``.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import re
from decimal import Decimal
from typing import Any

__all__ = [
    "MAX_CHARS", "to_jsonable", "mask_secrets", "dumps_truncated",
    "request_meta",
]

MAX_CHARS = 15_000     # CLOB payload cap (DB API request body limit is 64 KB: input+output+meta)

_FILE_TYPE_NAMES = {
    "UploadFile", "SpooledTemporaryFile", "TemporaryFile",
    "InMemoryUploadedFile", "SimpleUploadedFile", "FileStorage", "File",
}
_SECRET_RE = re.compile(
    r"\b(api[_\-]?key|token|secret|password|passwd|authorization|credential)\b", re.I)


# --- json conversion -------------------------------------------------------- #

def _file_placeholder(v: Any) -> dict:
    info: dict = {"type": "file", "content": "<binary omitted>"}
    name = getattr(v, "filename", None)
    if not isinstance(name, str) or not name:
        name = getattr(v, "name", None)
    if isinstance(name, str) and name:
        info["filename"] = name
    size = getattr(v, "size", None)
    if not isinstance(size, int):
        f = getattr(v, "file", None)
        if f is not None:
            try:
                pos = f.tell(); f.seek(0, 2)
                size = f.tell(); f.seek(pos)
            except Exception:
                size = None
    if isinstance(size, int):
        info["size"] = size
    return info


def to_jsonable(v: Any) -> Any:
    """Best-effort conversion to JSON-friendly data (binaries → placeholder).

    Handles: primitives, bytes, upload/file objects, list/tuple/set, dict,
    pydantic models (``model_dump``), dataclasses, datetime/date/time,
    Decimal, and any object with ``__dict__``.
    """
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<binary {len(v)} bytes omitted>"
    if type(v).__name__ in _FILE_TYPE_NAMES:
        return _file_placeholder(v)
    if isinstance(v, (list, tuple, set)):
        return [to_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): to_jsonable(x) for k, x in v.items()}
    if hasattr(v, "model_dump"):                       # pydantic v2
        return to_jsonable(v.model_dump())
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return to_jsonable(dataclasses.asdict(v))
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if hasattr(v, "__dict__"):
        return to_jsonable(vars(v))
    return str(v)


# --- secret masking ---------------------------------------------------------- #

def mask_secrets(v: Any) -> Any:
    """Recursively replace values whose KEY matches a secret pattern with "***"."""
    if isinstance(v, dict):
        return {k: ("***" if _SECRET_RE.search(str(k)) else mask_secrets(x))
                for k, x in v.items()}
    if isinstance(v, list):
        return [mask_secrets(x) for x in v]
    return v


def dumps_truncated(v: Any, max_chars: int = MAX_CHARS) -> str:
    """JSON-serialize (masked + jsonable), capped at ``max_chars``."""
    s = json.dumps(mask_secrets(to_jsonable(v)), ensure_ascii=False,
                   default=str, sort_keys=True)
    if len(s) > max_chars:
        s = s[:max_chars] + "... [truncated: payload was longer]"
    return s


# --- HTTP request snapshot ---------------------------------------------------- #

def is_request(v: Any) -> bool:
    return type(v).__name__ == "Request"


def request_meta(request: Any) -> dict:
    """Small, safe snapshot of the HTTP request (no body, no secrets)."""
    if request is None:
        return {}
    meta: dict = {}
    try:
        meta["method"] = request.method
        meta["path"] = request.url.path
        ct = request.headers.get("content-type")
        if ct:
            meta["content_type"] = ct
        qp = getattr(request, "query_params", None)
        if qp is not None:
            meta["query"] = dict(qp.items())
        keep = ("x-mr-no", "x-org", "x-zon", "x-loc", "x-user",
                "x-task-id", "x-request-id", "user-agent")
        meta["headers"] = {k: v for k, v in request.headers.items()
                           if k.lower() in keep}
    except Exception:
        pass
    return {k: v for k, v in meta.items() if v}
