"""shopfront -- a small stdlib-only product catalogue and order API."""

from __future__ import annotations

import sqlite3

__version__ = "1.4.2"

#: Applied lazily by :func:`connect`. Kept here because db.py, api.py and the
#: tests all need to agree on it, and nobody remembers to update two places.
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sku       TEXT    NOT NULL UNIQUE,
    name      TEXT    NOT NULL,
    price_cents INTEGER NOT NULL,
    stock     INTEGER NOT NULL DEFAULT 0,
    active    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS order_lines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    qty        INTEGER NOT NULL,
    unit_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'customer'
);
"""


def connect(db_path: str = "shopfront.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
