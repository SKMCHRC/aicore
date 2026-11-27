"""Oracle database layer for aicore.

One lazily-created connection pool shared by everything in the package.

Public API (`from aicore.db import ...`):
    query(sql, params=None)          → QueryResult   (SELECT — read-only, no commit)
    execute(sql, params=None)        → int rowcount  (INSERT/UPDATE/DELETE — auto-commit)
    execute_in_session(statements)   → (cols, rows)  (for CURRVAL-style sequences)
    pool()                           → the oracledb pool (lazy)
    QueryResult                      → .columns, .rows, .first, .dicts(), len(), iteration
    DbConfigError                    → missing/invalid DB_* env vars

Internal (package use — do not import from app code):
    _execute(sql, params) → (columns, rows)   (raw path, commits non-SELECTs)

Behaviour:
    * ``execute`` COMMITs (oracledb pools do NOT auto-commit) and returns the
      affected row count.
    * Broken pool connections are dropped (``pool.drop``) on error
      — NOTE: ``pool.close(conn)`` would close the WHOLE pool.
    * oracledb thin mode by default; set ``ORACLE_CLIENT_LIB_DIR`` to a valid
      instant-client dir to enable thick mode.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

__all__ = [
    "DbConfigError", "pool", "query", "execute", "execute_in_session",
    "QueryResult",
]

log = logging.getLogger("aicore.db")


class DbConfigError(RuntimeError):
    """Missing/invalid DB_* environment variables."""


def _db_settings() -> dict:
    missing = [
        name for name, val in (
            ("DB_HOST", os.getenv("DB_HOST")),
            ("DB_SERVICE_NAME", os.getenv("DB_SERVICE_NAME")),
            ("DB_USER", os.getenv("DB_USER")),
            ("DB_PASSWORD", os.getenv("DB_PASSWORD")),
        ) if not val
    ]
    if missing:
        raise DbConfigError("Missing DB env vars: " + ", ".join(missing))
    return {
        "host": os.environ["DB_HOST"],
        "port": int(os.getenv("DB_PORT", "1521")),
        "service_name": os.environ["DB_SERVICE_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }


_POOL = None


def pool():
    """Lazily create (and then reuse) the shared connection pool."""
    global _POOL
    if _POOL is None:
        import oracledb  # deferred: only needed when actually talking to a DB
        cfg = _db_settings()
        lib_dir = os.getenv("ORACLE_CLIENT_LIB_DIR")
        if lib_dir and os.path.isdir(lib_dir):
            oracledb.init_oracle_client(lib_dir=os.path.abspath(lib_dir))
            log.info("using Oracle thick client: %s", os.path.abspath(lib_dir))
        _POOL = oracledb.create_pool(
            user=cfg["user"],
            password=cfg["password"],
            dsn=oracledb.makedsn(cfg["host"], cfg["port"],
                                 service_name=cfg["service_name"]),
            min=1, max=5, timeout=60,
        )
    return _POOL


def _norm(cols: list) -> list:
    """Uniform lowercase identifiers (Oracle returns UPPER_CASE aliases)."""
    return [str(c).lower() for c in cols]


def _execute(query: str, params: Optional[dict] = None):
    """Raw path (INTERNAL): run one SQL → ``(columns, rows)``.

    * SELECT  → (lowercased column names, list-of-list rows)
    * non-SELECT → (['rows_affected'], [[n]]) + explicit COMMIT
    * on error the broken connection is dropped from the pool and the
      exception re-raised to the caller.
    """
    pool_obj = pool()
    conn = pool_obj.acquire()
    try:
        cur = conn.cursor()
        cur.execute(query, params or {})
        if cur.description:
            columns = _norm([d[0] for d in cur.description])
            rows = [list(r) for r in cur.fetchall()]
        else:
            columns, rows = ["rows_affected"], [[cur.rowcount]]
            conn.commit()
        cur.close()
        return columns, rows
    except Exception:
        try:
            pool_obj.drop(conn)          # NOTE: pool.close(conn) closes the whole pool
        except Exception:
            pass
        raise
    else:
        pool_obj.release(conn)


def execute_in_session(statements: List[tuple]) -> (list, list):
    """Run several statements in ONE session, then commit.

    Needed to read a sequence's ``CURRVAL`` right after an INSERT.
    Returns the LAST statement's ``(columns, rows)``.
    """
    pool_obj = pool()
    conn = pool_obj.acquire()
    try:
        cols, rows = ["rows_affected"], [[0]]
        for query, params in statements:
            cur = conn.cursor()
            cur.execute(query, params or {})
            if cur.description:
                cols = _norm([d[0] for d in cur.description])
                rows = [list(r) for r in cur.fetchall()]
            else:
                cols, rows = ["rows_affected"], [[cur.rowcount]]
            cur.close()
        conn.commit()
        return cols, rows
    except Exception:
        try:
            pool_obj.drop(conn)          # NOTE: pool.close(conn) closes the whole pool
        except Exception:
            pass
        raise
    else:
        pool_obj.release(conn)


# --------------------------------------------------------------------------- #
# Public read / write API (the thing teammates import)
# --------------------------------------------------------------------------- #

class QueryResult:
    """Typed result of ``query()`` — a SELECT's rows.

    ``r.columns``  → list of lowercased column names
    ``r.rows``     → list of tuples
    ``r.first``    → first row (raises LookupError on an empty set)
    ``r.dicts()``  → list of {column: value} dicts
    ``len(r)``, ``r[0]``, ``for row in r`` all work
    """
    __slots__ = ("columns", "rows")

    def __init__(self, columns: list, rows: list):
        self.columns = [str(c).lower() for c in columns]
        self.rows = [tuple(r) for r in rows]

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def first(self):
        if not self.rows:
            raise LookupError("query() returned an empty result set")
        return self.rows[0]

    def dicts(self) -> list:
        """One {column_name: value} dict per row — the easiest way to consume."""
        return [dict(zip(self.columns, r)) for r in self.rows]

    def __len__(self):
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def __getitem__(self, i):
        return self.rows[i]

    def __repr__(self):
        return f"QueryResult(columns={self.columns}, {len(self.rows)} row(s))"


def query(sql: str, params: Optional[dict] = None) -> QueryResult:
    """Run a SELECT and return its rows as a ``QueryResult`` (no commit).

        r = query("SELECT * FROM myschema.my_table WHERE active = :a",
                  {"a": "Y"})
        r.columns, r.first, r.dicts(), len(r), r[0][1] ...

    Use ``execute()`` for INSERT/UPDATE/DELETE.
    """
    cols, rows = _execute(sql, params)
    if cols == ["rows_affected"]:
        raise ValueError(
            "query() is for SELECT statements — you passed a write "
            "(INSERT/UPDATE/DELETE). Use aicore.db.execute() instead.")
    return QueryResult(cols, rows)


def execute(sql: str, params: Optional[dict] = None) -> int:
    """Run an INSERT/UPDATE/DELETE — auto-commits — and return the rowcount.

        n = execute("INSERT INTO MY_TABLE (id, note) VALUES (:id, :n)",
                    {"id": 7, "n": "hi"})

    Use ``query()`` for SELECT.
    """
    cols, rows = _execute(sql, params)
    if cols != ["rows_affected"]:
        raise ValueError(
            "execute() is for INSERT/UPDATE/DELETE — your statement returned "
            "a result set (SELECT). Use aicore.db.query() instead.")
    return rows[0][0]


# --------------------------------------------------------------------------- #
# backward-compat aliases (old single-file ai_core exposed these private names)
# --------------------------------------------------------------------------- #
_pool = pool
_execute_in_session = execute_in_session
