"""Environment handling for aicore (`.env` loading).

Loads the DB credentials the Oracle layer needs. Resolution order for the
`.env` file:

1. ``$LIB_AI_ENV_FILE``  — explicit override (path to a .env file)
2. ``<project>/.env``    — one level above ``aicore`` (i.e. next to ``lib/``)
3. ``./.env``            — current working directory

Existing environment variables are NEVER overridden (``os.environ.setdefault``),
so real environment always wins over file values.
"""
from __future__ import annotations

import os

__all__ = ["load_env", "get_app_id", "AppIdMissingError"]

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))

_LOADED = False


def _candidates() -> list:
    out = []
    override = os.getenv("LIB_AI_ENV_FILE")
    if override:
        out.append(override)
    out.append(os.path.normpath(os.path.join(_PKG_DIR, "..", "..", ".env")))
    out.append(os.path.join(os.getcwd(), ".env"))
    return out


def load_env(force: bool = False) -> None:
    """Load .env values into ``os.environ`` (idempotent; ``override=False``).

    Uses ``python-dotenv`` when available, otherwise a tiny built-in parser
    (KEY=VALUE lines, ``#`` comments, optional single/double quotes).
    """
    global _LOADED
    if _LOADED and not force:
        return

    paths = [p for p in _candidates() if os.path.isfile(p)]
    try:
        from dotenv import load_dotenv                     # type: ignore
        for path in paths:
            load_dotenv(path, override=False)
    except ImportError:
        for path in paths:
            _fallback_load(path)
    _LOADED = True


def _fallback_load(path: str) -> None:
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip("'\"")
            os.environ.setdefault(k.strip(), v)


class AppIdMissingError(RuntimeError):
    """No app id given and ``APP_ID`` is not set in the environment / .env."""


def get_app_id(app_id=None) -> int:
    """Return the app id: the explicit value if given, else ``APP_ID`` from
    the environment (``.env`` is loaded automatically).  Raises
    ``AppIdMissingError`` if neither is available, ``ValueError`` if it is
    not an integer."""
    load_env()
    raw = app_id if app_id is not None else os.getenv("APP_ID")
    if raw is None or str(raw).strip() == "":
        raise AppIdMissingError(
            "app id is not set — add APP_ID=<your app id> to your .env "
            "(or the environment)")
    try:
        return int(str(raw).strip())
    except ValueError:
        raise ValueError(f"app id must be an integer, got {raw!r}") from None
