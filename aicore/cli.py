"""aicore CLI — quick model-config lookup.

Usage:
    python -m aicore <purpose>              (app id from APP_ID in .env)
    python -m aicore <app_id> <purpose>     (explicit app id)

Examples:
    python -m aicore GENERATE_MDT
    python -m aicore 3 GENERATE_MDT
"""
from __future__ import annotations

import sys
from typing import List, Optional

from .models import (
    AmbiguousModelError, AppNotRegisteredError, ModelLookupError,
    get_model_config,
)
from .db import DbConfigError
from .env import AppIdMissingError


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if len(argv) not in (1, 2):
        print(__doc__)
        return 1

    purpose = argv[-1]
    try:
        app_id = int(argv[0]) if len(argv) == 2 else None
    except ValueError:
        print("ERROR: app_id must be an integer", file=sys.stderr)
        return 1

    try:
        cfg = get_model_config(app_id, purpose)
    except (ModelLookupError, AppNotRegisteredError, AppIdMissingError,
            AmbiguousModelError, DbConfigError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    for k in ("label", "model_name", "model_type", "provider", "url",
              "temperature", "max_retries", "keep_alive", "context_size",
              "num_predict", "active", "remarks"):
        print(f"{k:>14} = {cfg.get(k)!r}")
    print(f"{'key meta':>14} = {cfg.get('api_key_meta')}")
    if cfg.get("api_key") is not None:
        print(f"{'api_key':>14} = *** (present)")
    else:
        print(f"{'api_key':>14} = (none — no PAID+active key for this provider)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
