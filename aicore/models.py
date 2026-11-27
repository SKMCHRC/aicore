"""AICORE model-config resolver.

``get_model_config(app_id, purpose)`` → the ONE model configuration for an
app + purpose, joined with the provider's LIVE PAID API key (if any).

Errors:
    ModelLookupError       — no config found for that (app_id, purpose)
    AppNotRegisteredError  — app_id not registered in the AICORE registry
    AmbiguousModelError    — data problem: more than 1 config row exists
    DbConfigError          — DB_API_URL / DB_ACCESS_KEY not configured
    DbApiError             — the DB Access API call failed
"""
from __future__ import annotations

from typing import Optional

from .dbapi import query as _api_query   # saved query via the DB Access API
from .env import get_app_id

__all__ = [
    "get_model_config", "get_model_conf",
    "ModelLookupError", "AppNotRegisteredError", "AmbiguousModelError",
]


class ModelLookupError(RuntimeError):
    """No AICORE model configured for (app_id, purpose)."""


class AppNotRegisteredError(RuntimeError):
    """app_id is not registered in the AICORE app registry."""


class AmbiguousModelError(RuntimeError):
    """More than one config row matched (app_id, purpose) — data problem."""


#: Saved query on the DB Access API (the SQL lives in the dashboard).
#: params: app_id, purpose → the ONE config row (app + task + model + provider
#: + the provider's live PAID api key).
_RESOLVE_QUERY = "model_config_by_app_purpose"


def get_model_config(app_id=None, purpose=None) -> dict:
    """Resolve the ONE model configuration for an app + purpose.

    ``app_id`` defaults to ``APP_ID`` from ``.env`` (error if unset), so the
    usual call is just ``get_model_config(purpose="GENERATE_MDT")``.

    >>> cfg = get_model_config(purpose="GENERATE_MDT")
    >>> cfg["model_name"], cfg["provider"], cfg["api_key"]
    >>> cfg["api_key_meta"]["email_id"]          # key redacted in meta

    Returns a plain dict:
        app_name, app_id, purpose, label,
        model_id, model_name, model_type, provider_id, provider,
        seed, url, max_retries, keep_alive, temperature,
        context_size, num_predict, remarks, active,
        is_ollama, is_gemini,               # handy provider booleans
        api_key,                            # LIVE key (None if provider has none)
        api_key_meta                        # {llm_api_id, email_id, api_type, max_hit}
                                           # (api_key redacted as "***")
    """
    if purpose is None and isinstance(app_id, str):    # get_model_config("PURPOSE")
        app_id, purpose = None, app_id
    app_id = get_app_id(app_id)
    purpose = (purpose or "").strip()
    if not purpose:
        raise ValueError("purpose must be a non-empty string")

    rows = _api_query(_RESOLVE_QUERY, {"app_id": app_id, "purpose": purpose}).dicts()
    if not rows:
        raise ModelLookupError(
            f"No AICORE model configured for app_id={app_id}, purpose={purpose!r}")
    if len(rows) > 1:
        raise AmbiguousModelError(
            f"{len(rows)} config rows for app_id={app_id}, purpose={purpose!r} "
            f"(expected exactly 1) — contact the platform team")

    data = rows[0]
    if data.get("app_name") is None:
        raise AppNotRegisteredError(
            f"app_id={app_id} is not registered in AICORE — contact the platform team")

    provider = (data.get("provider") or "").upper()
    cfg = {
        "app_name":       data["app_name"],
        "app_id":         data["app_id"],
        "purpose":        data["purpose"],
        "label":          f"{data['app_name']} / {data['purpose']}",
        "model_id":       data["model_id"],
        "model_name":     data["model_name"],
        "model_type":     data["model_type"],
        "provider_id":    data["provider_id"],
        "provider":       provider,
        "seed":           data.get("seed"),
        "url":            data.get("url"),
        "max_retries":    data.get("max_retries"),
        "keep_alive":     data.get("keep_alive"),
        "temperature":    data.get("temperature"),
        "context_size":   data.get("context_size"),
        "num_predict":    data.get("num_predict"),
        "remarks":        data.get("remarks"),
        "active":         data.get("active"),
        "is_ollama":      provider == "OLLAMA",
        "is_gemini":      provider == "GEMINI",
        # the LIVE key your app should use (None if the provider has none yet)
        "api_key":        data.get("api_key"),
        "api_key_meta":   (
            {
                "llm_api_id": data["api_key_id"],
                "api_key":    "***",                    # redacted in meta
                "email_id":   data.get("api_key_email_id"),
                "api_type":   (data.get("api_key_type") or "").strip().upper() or None,
                "max_hit":    data.get("api_key_max_hit"),
            }
            if data.get("api_key") is not None else None),
    }
    # tidy None-ish strings
    return {k: (v.strip() if isinstance(v, str) and v is not None else v)
            for k, v in cfg.items()}


# friendly alias
get_model_conf = get_model_config
