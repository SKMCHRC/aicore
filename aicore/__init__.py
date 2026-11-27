"""aicore — AICORE model-config resolution + AI request tracking.

Public API (re-exported here — import from ``aicore``):

    get_model_config(purpose, app_id=None)   → dict  (app_id defaults to APP_ID in .env)   (alias: get_model_conf)
    @ai_history(task_type=...)   → endpoint tracking decorator

Exceptions:
    ModelLookupError, AppNotRegisteredError, AmbiguousModelError
    AiHistoryFormatError, AIHistoryContextError
    DbConfigError, AppIdMissingError

Lower-level pieces (import from their modules):
    aicore.dbapi      → query(), execute(), custom_query(), custom_execute(),
                        configure(), DbApiError   (DB Access API over HTTP)
    aicore.db         → DIRECT Oracle: query(sql), execute(sql), execute_in_session(),
                        pool(), DbConfigError  (internal use — aicore's own AICORE tables)
    aicore.serialize  → to_jsonable(), mask_secrets(), dumps_truncated()
    aicore.env        → load_env()

CLI:
    python -m aicore <purpose>      (APP_ID from .env)

Full usage guide (for humans AND AI agents): see ``aicore/SKILL.md``.
"""
from __future__ import annotations

from .env import AppIdMissingError, get_app_id, load_env
load_env()   # pick up .env credentials at import time (idempotent)

from .db import DbConfigError, QueryResult
from .dbapi import (
    ApiResult, DbApiError, custom_execute, custom_query, execute, query,
)
from .dbapi import configure as configure_db_api
from .models import (
    AmbiguousModelError, AppNotRegisteredError, ModelLookupError,
    get_model_conf, get_model_config,
)
from .history import (
    AI_HISTORY_REQUIRED_FIELDS, AIHistoryContextError, AiHistoryFormatError,
    ai_history,
)
from .serialize import dumps_truncated, mask_secrets, request_meta, to_jsonable
from .health import (
    URLS_QUERY, add_health_endpoint, app_provider_urls, check_reachability,
    health_payload, health_router,
)

__version__ = "4.0.0"

__all__ = [
    # resolver
    "get_model_config", "get_model_conf",
    "ModelLookupError", "AppNotRegisteredError", "AmbiguousModelError",
    # tracking
    "ai_history",
    "AiHistoryFormatError", "AIHistoryContextError", "AI_HISTORY_REQUIRED_FIELDS",
    # db layer
    # (query/execute = DB Access API by saved-query NAME; direct SQL lives in aicore.db)
    "query", "execute", "custom_query", "custom_execute", "configure_db_api",
    "DbApiError", "DbConfigError", "QueryResult", "ApiResult",
    # serialization helpers
    "to_jsonable", "mask_secrets", "dumps_truncated", "request_meta",
    # health (every app MUST expose GET /health — wire it up with add_health_endpoint)
    "add_health_endpoint", "health_router", "health_payload", "check_reachability",
    "URLS_QUERY", "app_provider_urls",
    # env
    "load_env", "get_app_id", "AppIdMissingError",
    "__version__",
]
