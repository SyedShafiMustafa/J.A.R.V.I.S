"""
server/memory.py
----------------
Basic server-side persistence for the JARVIS V2 server (Phase 1).

Two things are stored today on the server database:
- conversations: per-device message history with timestamps and metadata
- devices: registered/paired devices with status and last-seen tracking

This module is intentionally backend-only. It does not import the legacy
voice runtime and does not depend on config/settings.py. All data paths
are derived from ServerConfig so the same code works in tests with temp dirs.

Schema is versioned in _meta so later phases can add migrations without
rewriting this module.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

MEMORY_SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ #
# schema
# ------------------------------------------------------------------ #


def _ensure_memory_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            token TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'paired',
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            last_message_at TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            metadata TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON conversation_messages(conversation_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_device ON conversations(device_id)"
    )


# ------------------------------------------------------------------ #
# connection helpers
# ------------------------------------------------------------------ #


def open_memory_db(cfg) -> sqlite3.Connection:
    conn = sqlite3.connect(str(cfg.db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    _ensure_memory_schema(conn)
    _ensure_memory_schema_version(conn)
    return conn


def _ensure_memory_schema_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO _meta (key, value) VALUES ('memory_schema_version', ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (str(MEMORY_SCHEMA_VERSION),),
    )
    conn.commit()


# ------------------------------------------------------------------ #
# devices
# ------------------------------------------------------------------ #


class DeviceStore:
    """Registered/paired devices on the server."""

    def __init__(self, cfg) -> None:
        self._cfg = cfg

    def _conn(self) -> sqlite3.Connection:
        return open_memory_db(self._cfg)

    def register(
        self,
        *,
        device_id: str,
        name: str,
        type: str,
        token: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not device_id:
            raise ValueError("device_id is required")
        if not name:
            raise ValueError("device_id requires a name")
        if not token:
            raise ValueError("device_id requires a token")

        now = now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO devices (id, name, type, token, created_at, last_seen_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    type = excluded.type,
                    token = excluded.token,
                    last_seen_at = excluded.last_seen_at,
                    metadata = excluded.metadata
                """,
                (
                    device_id,
                    name,
                    type,
                    token,
                    now,
                    now,
                    json.dumps(metadata or {}),
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
            assert row is not None
            return _device_row_to_dict(row)

    def get(self, device_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
            if row is None:
                return None
            return _device_row_to_dict(row)

    def touch(self, device_id: str) -> None:
        now = now_iso()
        with self._conn() as conn:
            conn.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?",
                (now, device_id),
            )
            conn.commit()

    def list_all(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM devices ORDER BY created_at").fetchall()
            return [_device_row_to_dict(row) for row in rows]

    def delete(self, device_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
            conn.commit()
            return cur.rowcount > 0


def _device_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "token": row["token"],
        "status": row["status"],
        "metadata": _json_safe(row["metadata"]),
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
    }


# ------------------------------------------------------------------ #
# conversations
# ------------------------------------------------------------------ #


class ConversationStore:
    """Per-device conversation history with lightweight metadata."""

    def __init__(self, cfg) -> None:
        self._cfg = cfg

    def _conn(self) -> sqlite3.Connection:
        return open_memory_db(self._cfg)

    def ensure_conversation(self, conversation_id: str, device_id: str) -> dict[str, Any]:
        now = now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO conversations (id, device_id, started_at, last_message_at, message_count, metadata)
                VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_message_at = excluded.last_message_at,
                    message_count = conversations.message_count + 1
                """,
                (conversation_id, device_id, now, now, json.dumps({})),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            assert row is not None
            return _conversation_row_to_dict(row)

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if row is None:
                return None
            return _conversation_row_to_dict(row)

    def list_for_device(self, device_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE device_id = ? ORDER BY last_message_at DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
            return [_conversation_row_to_dict(row) for row in rows]

    def save_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not conversation_id:
            raise ValueError("conversation_id is required")
        if not role:
            raise ValueError("role is required")
        if content is None:
            content = ""

        now = now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO conversation_messages (conversation_id, role, content, created_at, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, role, content, now, json.dumps(metadata or {})),
            )
            conn.execute(
                "UPDATE conversations SET last_message_at = ?, message_count = message_count + 1 WHERE id = ?",
                (now, conversation_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM conversation_messages WHERE rowid = last_insert_rowid()"
            ).fetchone()
            assert row is not None
            return _message_row_to_dict(row)

    def list_messages(
        self,
        conversation_id: str,
        limit: int = 200,
        after_id: int | None = None,
    ) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if after_id is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM conversation_messages
                    WHERE conversation_id = ? AND id > ? ORDER BY id ASC LIMIT ?
                    """,
                    (conversation_id, after_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM conversation_messages
                    WHERE conversation_id = ? ORDER BY id ASC LIMIT ?
                    """,
                    (conversation_id, limit),
                ).fetchall()
            return [_message_row_to_dict(row) for row in rows]

    def delete(self, conversation_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            conn.commit()
            return cur.rowcount > 0


def _conversation_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "started_at": row["started_at"],
        "last_message_at": row["last_message_at"],
        "message_count": row["message_count"],
        "metadata": _json_safe(row["metadata"]),
    }


def _message_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "role": row["role"],
        "content": row["content"],
        "created_at": row["created_at"],
        "metadata": _json_safe(row["metadata"]),
    }


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except Exception:
        return value
