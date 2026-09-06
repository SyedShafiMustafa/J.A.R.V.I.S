"""
server/db.py
------------
SQLite bootstrap for the JARVIS V2 Dell server (stdlib sqlite3 only).

Phase 0 keeps this minimal on purpose:
    - data directory + database file creation
    - WAL journal mode and foreign keys enabled per connection
    - a `_meta` table holding the schema version

Later phases add real tables through versioned migrations driven by
`SCHEMA_VERSION`, so the schema grows without ad-hoc table scripts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1


def connect(db_path: str | Path, timeout: float = 10.0) -> sqlite3.Connection:
    """Open a connection with the project's standard pragmas applied."""
    conn = sqlite3.connect(str(db_path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def ensure_db(cfg) -> Path:
    """Create data dirs + the `_meta` table. Returns the db path. Idempotent."""
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(cfg.db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _meta ("
            " key TEXT PRIMARY KEY,"
            " value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO _meta (key, value) VALUES ('schema_version', ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    finally:
        conn.close()
    return cfg.db_path


def db_ok(cfg) -> bool:
    """Readiness probe: can we open the db and read the schema version?"""
    try:
        conn = connect(cfg.db_path)
        try:
            row = conn.execute(
                "SELECT value FROM _meta WHERE key = 'schema_version'"
            ).fetchone()
            return row is not None and row["value"] == str(SCHEMA_VERSION)
        finally:
            conn.close()
    except sqlite3.Error:
        return False
