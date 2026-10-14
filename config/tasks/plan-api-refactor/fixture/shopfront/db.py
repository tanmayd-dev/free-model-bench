"""Ad-hoc sqlite helpers.

Every call opens a fresh connection and closes it, which keeps call sites
simple and costs a full connect/`executescript` round trip per query.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from shopfront import connect as _connect


def query(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    conn = _connect()
    try:
        conn.execute(sql, tuple(params))
        conn.commit()
        cur = conn.execute("SELECT last_insert_rowid()")
        return int(cur.fetchone()[0])
    finally:
        conn.close()


def scalar(sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
    row = one(sql, params)
    if not row:
        return default
    return next(iter(row.values()))
