import threading
import sqlite3
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path

from config.settings import MEMORY_DB_PATH

# Import models for type hints
from backend.models import Conversation, Message


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    # Existing memories table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_message TEXT,
            assistant_message TEXT
        )
    """)

    # New: conversations table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            last_message_at TEXT NOT NULL,
            message_count INTEGER DEFAULT 0,
            metadata TEXT DEFAULT '{}'
        )
    """)

    # New: messages table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
        )
    """)

    # Indexes
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at)"
    )

    conn.commit()


class Memory:
    def __init__(self):
        self.db_path = MEMORY_DB_PATH

        self.conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False
        )

        self.lock = threading.Lock()
        self.cursor = self.conn.cursor()

        _ensure_schema(self.conn)

    # --- Existing memory methods ---

    def save_memory(self, user_message, assistant_message):
        with self.lock:
            self.cursor.execute(
                "INSERT INTO memories (user_message, assistant_message) VALUES (?, ?)",
                (user_message, assistant_message)
            )
            self.conn.commit()

    def search_memories(self, query):
        with self.lock:
            words = query.lower().replace("?", "").split()
            results = []

            for word in words:
                self.cursor.execute(
                    """
                    SELECT user_message, assistant_message
                    FROM memories
                    WHERE LOWER(user_message) LIKE ?
                    """,
                    (f"%{word}%",)
                )
                results.extend(self.cursor.fetchall())

        return list(dict.fromkeys(results))

    # --- New conversation persistence methods ---

    def create_conversation(self) -> str:
        """Create a new conversation and return its ID."""
        conversation_id = uuid.uuid4().hex
        now = _iso_now()
        with self.lock:
            self.cursor.execute(
                """
                INSERT INTO conversations (id, started_at, last_message_at, message_count)
                VALUES (?, ?, ?, 0)
                """,
                (conversation_id, now, now)
            )
            self.conn.commit()
        return conversation_id

    def get_conversation(self, conversation_id: str) -> dict | None:
        """Get conversation metadata by ID."""
        with self.lock:
            row = self.cursor.execute(
                "SELECT id, started_at, last_message_at, message_count, metadata FROM conversations WHERE id = ?",
                (conversation_id,)
            ).fetchone()
            if row is None:
                return None
            return {
                "id": row[0],
                "started_at": row[1],
                "last_message_at": row[2],
                "message_count": row[3],
                "metadata": json.loads(row[4]) if row[4] else {},
            }

    def get_last_conversation(self) -> dict | None:
        """Get the most recent conversation."""
        with self.lock:
            row = self.cursor.execute(
                "SELECT id, started_at, last_message_at, message_count, metadata FROM conversations ORDER BY last_message_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            return {
                "id": row[0],
                "started_at": row[1],
                "last_message_at": row[2],
                "message_count": row[3],
                "metadata": json.loads(row[4]) if row[4] else {},
            }

    def save_message(self, conversation_id: str, role: str, content: str, metadata: dict | None = None) -> int:
        """Save a message and update conversation timestamp/count."""
        now = _iso_now()
        metadata_json = json.dumps(metadata or {})
        with self.lock:
            cursor = self.cursor.execute(
                """
                INSERT INTO messages (conversation_id, role, content, created_at, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, role, content, now, metadata_json)
            )
            message_id = cursor.lastrowid
            self.cursor.execute(
                """
                UPDATE conversations
                SET last_message_at = ?, message_count = message_count + 1
                WHERE id = ?
                """,
                (now, conversation_id)
            )
            self.conn.commit()
        return message_id

    def get_messages(self, conversation_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
        """Get messages for a conversation."""
        with self.lock:
            rows = self.cursor.execute(
                """
                SELECT id, conversation_id, role, content, created_at, metadata
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at ASC
                LIMIT ? OFFSET ?
                """,
                (conversation_id, limit, offset)
            ).fetchall()
            return [
                {
                    "id": row[0],
                    "conversation_id": row[1],
                    "role": row[2],
                    "content": row[3],
                    "created_at": row[4],
                    "metadata": json.loads(row[5]) if row[5] else {},
                }
                for row in rows
            ]

    def get_message_count(self, conversation_id: str) -> int:
        """Get message count for a conversation."""
        with self.lock:
            row = self.cursor.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (conversation_id,)
            ).fetchone()
            return row[0] if row else 0

    def list_conversations(self, limit: int = 20) -> list[dict]:
        """List recent conversations."""
        with self.lock:
            rows = self.cursor.execute(
                "SELECT id, started_at, last_message_at, message_count, metadata FROM conversations ORDER BY last_message_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [
                {
                    "id": row[0],
                    "started_at": row[1],
                    "last_message_at": row[2],
                    "message_count": row[3],
                    "metadata": json.loads(row[4]) if row[4] else {},
                }
                for row in rows
            ]

    # --- Existing memory methods ---

    def save_memory(self, user_message, assistant_message):
        with self.lock:
            self.cursor.execute(
                "INSERT INTO memories (user_message, assistant_message) VALUES (?, ?)",
                (user_message, assistant_message)
            )
            self.conn.commit()

    def search_memories(self, query):
        with self.lock:
            words = query.lower().replace("?", "").split()
            results = []

            for word in words:
                self.cursor.execute(
                    """
                    SELECT user_message, assistant_message
                    FROM memories
                    WHERE LOWER(user_message) LIKE ?
                    """,
                    (f"%{word}%",)
                )
                results.extend(self.cursor.fetchall())

        return list(dict.fromkeys(results))