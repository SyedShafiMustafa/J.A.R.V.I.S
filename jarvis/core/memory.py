import threading
import sqlite3
import uuid
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from config.settings import MEMORY_DB_PATH

# Import models for type hints
from backend.models import Conversation, Message
from agents.ollama_errors import OllamaError

_log = logging.getLogger("jarvis.memory")


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

    # New: conversation summaries table (PR3)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            summary_text TEXT NOT NULL,
            covered_message_start INTEGER NOT NULL,
            covered_message_end INTEGER NOT NULL,
            created_at TEXT NOT NULL,
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_summaries_conversation ON conversation_summaries(conversation_id)"
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

    # --- Summarization Engine (PR3) ---

    def _get_latest_summary(self, conversation_id: str) -> dict | None:
        """Get the latest summary for a conversation."""
        with self.lock:
            row = self.cursor.execute(
                """
                SELECT id, conversation_id, summary_text, covered_message_start, covered_message_end, created_at
                FROM conversation_summaries
                WHERE conversation_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (conversation_id,)
            ).fetchone()
            if row is None:
                return None
            return {
                "id": row[0],
                "conversation_id": row[1],
                "summary_text": row[2],
                "covered_message_start": row[3],
                "covered_message_end": row[4],
                "created_at": row[5],
            }

    def _save_summary(self, conversation_id: str, summary_text: str, start_msg_id: int, end_msg_id: int) -> int:
        """Save a conversation summary."""
        now = _iso_now()
        with self.lock:
            cursor = self.cursor.execute(
                """
                INSERT INTO conversation_summaries (conversation_id, summary_text, covered_message_start, covered_message_end, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, summary_text, start_msg_id, end_msg_id, now)
            )
            summary_id = cursor.lastrowid
            self.conn.commit()
        return summary_id

    def _should_summarize(self, conversation_id: str) -> tuple[bool, int, int]:
        """
        Check if conversation needs summarization.
        Returns (should_summarize, start_position, end_position) where positions are 1-based offsets.
        """
        total_messages = self.get_message_count(conversation_id)
        
        # Need at least 60 messages (30 turns) to trigger summarization
        if total_messages < 60:
            return False, 0, 0
        
        # Check if we already have a summary covering recent messages
        latest_summary = self._get_latest_summary(conversation_id)
        if latest_summary:
            # Get the message ID that the latest summary ends at
            covered_end = latest_summary["covered_message_end"]
            # Find the position (offset) of this message
            with self.lock:
                pos_row = self.cursor.execute(
                    """
                    SELECT COUNT(*) FROM messages
                    WHERE conversation_id = ? AND id <= ?
                    """,
                    (conversation_id, covered_end)
                ).fetchone()
            covered_end_pos = pos_row[0] if pos_row else 0
            
            # We need at least 20 new turns (40 messages) since last summary
            messages_since_summary = total_messages - covered_end_pos
            if messages_since_summary < 40:
                return False, 0, 0
        
        # Summarize oldest 40 messages (20 turns), keep newest 20 messages (10 turns)
        # start_pos = 1 (1-based), end_pos = 40
        return True, 1, 40

    def maybe_summarize(self, conversation_id: str) -> bool:
        """
        Check if conversation needs summarization and generate if needed.
        Returns True if summarization was performed.
        """
        should_summarize, start_pos, end_pos = self._should_summarize(conversation_id)
        if not should_summarize:
            return False
        
        # Get messages to summarize by position (offset/limit) not by ID
        with self.lock:
            rows = self.cursor.execute(
                """
                SELECT id, role, content FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at ASC
                LIMIT ? OFFSET ?
                """,
                (conversation_id, end_pos - start_pos + 1, start_pos - 1)
            ).fetchall()
        
        if not rows:
            return False
        
        # Build conversation text for summarization
        conversation_text = "\n".join(
            f"{'User' if row[1] == 'user' else 'Assistant'}: {row[2]}"
            for row in rows
        )
        
        # Generate summary using the brain
        summary_text = self._generate_summary(conversation_text)
        if not summary_text:
            return False
        
        # Save summary with actual message IDs
        self._save_summary(conversation_id, summary_text, rows[0][0], rows[-1][0])
        return True

    def _generate_summary(self, conversation_text: str) -> str | None:
        """Generate a summary of the conversation using the LLM."""
        try:
            from agents.brain import JarvisBrain
            brain = JarvisBrain()
            
            summarization_prompt = f"""Summarize the following conversation in a concise way that preserves:
- Key decisions made
- Project names and technical details
- Unresolved tasks or open questions
- Important context for continuity

Maximum 250 tokens. Be concise but thorough.

Conversation:
{conversation_text}

Summary:"""
            
            messages = [
                {"role": "system", "content": "You are a helpful assistant that creates concise conversation summaries."},
                {"role": "user", "content": summarization_prompt}
            ]
            
            summary_parts = list(brain.stream(messages))
            return " ".join(summary_parts).strip()
        except OllamaError as exc:
            _log.warning("conversation summary unavailable: %s", type(exc).__name__)
            return None
        except Exception:
            _log.exception("conversation summary generation failed")
            return None

    # --- Context Window Engine ---

    def get_recent_messages(self, conversation_id: str, limit: int = 15) -> list[dict]:
        """
        Get the most recent messages for a conversation.
        Returns messages in chronological order (oldest first).
        """
        with self.lock:
            rows = self.cursor.execute(
                """
                SELECT id, conversation_id, role, content, created_at, metadata
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (conversation_id, limit)
            ).fetchall()
            # Reverse to get chronological order (oldest first)
            rows.reverse()
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

    def build_context(
        self,
        conversation_id: str,
        current_message: str,
        system_prompt: str = "You are JARVIS, a fast desktop AI assistant.\nReply naturally in 1-2 sentences unless asked otherwise.",
        max_turns: int = 10,  # Keep only 10 recent turns raw
    ) -> list[dict]:
        """
        Build the full context for LLM request.
        Returns a list of message dicts in the format expected by the LLM API.
        Now includes conversation summary if available.
        """
        # DO NOT call maybe_summarize here - it should be called after a complete turn
        # Get the latest summary
        summary = self._get_latest_summary(conversation_id)
        
        # Get recent messages (up to max_turns * 2 messages = max_turns turns)
        recent_messages = self.get_recent_messages(conversation_id, limit=max_turns * 2)

        # Build messages list
        messages = [
            {"role": "system", "content": system_prompt},
        ]

        # Add summary if available
        if summary:
            messages.append({
                "role": "system",
                "content": f"Conversation summary (earlier context): {summary['summary_text']}",
            })

        # Add recent conversation history
        for msg in recent_messages:
            messages.append({
                "role": msg["role"],
                "content": msg["content"],
            })

        # Add current user message
        messages.append({"role": "user", "content": current_message})

        return messages

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