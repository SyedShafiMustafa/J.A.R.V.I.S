"""
core/permission.py

Centralized Permission Engine and Persistent Confirmation Store for J.A.R.V.I.S.

Differentiates between:
- Harmless / Read-only operations (immediate execution)
- Normal user-requested actions (immediate execution when explicitly requested)
- Sensitive operations (system changes, credentials, external mutations)
- Destructive operations (deleting files, dropping databases, system shutdown)

Provides a persistent SQLite confirmation store so pending actions survive restarts,
resume cleanly upon approval, and cancel reliably on rejection.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from config.settings import MEMORY_DB_PATH
from backend.interfaces import ToolResult

_log = logging.getLogger("jarvis.permission")


class PermissionLevel(str, Enum):
    HARMLESS = "harmless"
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    DESTRUCTIVE = "destructive"


class ActionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


# Tool classification table
_TOOL_PERMISSIONS: dict[str, PermissionLevel] = {
    "open_app": PermissionLevel.NORMAL,
    "close_app": PermissionLevel.NORMAL,
    "wait_window": PermissionLevel.HARMLESS,
    "open_youtube": PermissionLevel.HARMLESS,
    "search_youtube": PermissionLevel.HARMLESS,
    "search_google": PermissionLevel.HARMLESS,
    "type": PermissionLevel.NORMAL,
    "press": PermissionLevel.HARMLESS,
    "hotkey": PermissionLevel.NORMAL,
    "click_text": PermissionLevel.NORMAL,
    # Visual computer agent (Milestone 3). Observation is read-only;
    # acting on the GUI is a normal user-requested action.
    "screenshot": PermissionLevel.HARMLESS,
    "inspect_screen": PermissionLevel.HARMLESS,
    "locate_target": PermissionLevel.HARMLESS,
    "visual_verify": PermissionLevel.HARMLESS,
    "visual_click": PermissionLevel.NORMAL,
    "visual_type": PermissionLevel.NORMAL,
    "visual_drag": PermissionLevel.NORMAL,
    "visual_scroll": PermissionLevel.NORMAL,
    "visual_menu": PermissionLevel.NORMAL,
    # Cross-application automation (Milestone 4). Window observation and
    # text extraction are read-only; acting across apps is normal.
    "list_windows": PermissionLevel.HARMLESS,
    "extract_window_text": PermissionLevel.HARMLESS,
    "read_clipboard": PermissionLevel.HARMLESS,
    "switch_app": PermissionLevel.NORMAL,
    "window_manage": PermissionLevel.NORMAL,
    "run_workflow": PermissionLevel.NORMAL,
    "send_whatsapp": PermissionLevel.NORMAL,
    # Windows settings (§29). Reads are read-only; reversible changes
    # are normal user-requested actions under the existing tiers —
    # no new tier is introduced.
    "get_setting": PermissionLevel.HARMLESS,
    "set_setting": PermissionLevel.NORMAL,
    "delete_file": PermissionLevel.DESTRUCTIVE,
    "execute_terminal": PermissionLevel.SENSITIVE,
    "system_shutdown": PermissionLevel.DESTRUCTIVE,
    # File-management agent. Read-only inspection is harmless; a batch move is
    # classified per payload (a preview never touches files).
    "analyze_directory": PermissionLevel.HARMLESS,
    "find_duplicates": PermissionLevel.HARMLESS,
    "organize_directory": PermissionLevel.NORMAL,
}

# Tools whose risk depends on the payload, not just the tool name.
# ``organize_directory`` is a preview (read-only) until ``dry_run`` is False.
# ``extract_window_text`` is pure OCR until ``method`` touches the clipboard.
_PAYLOAD_AWARE_PERMISSIONS = {
    "organize_directory": lambda payload: (
        PermissionLevel.HARMLESS
        if payload.get("dry_run", True)
        else PermissionLevel.NORMAL
    ),
    "extract_window_text": lambda payload: (
        PermissionLevel.HARMLESS
        if payload.get("method", "ocr") == "ocr"
        else PermissionLevel.NORMAL
    ),
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_permission_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_actions (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            task_id TEXT,
            tool TEXT NOT NULL,
            payload TEXT NOT NULL,
            permission_level TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            result_data TEXT DEFAULT '{}'
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_actions_status ON pending_actions(status)"
    )
    conn.commit()


class PermissionEngine:
    """Evaluates whether tool executions require explicit confirmation."""

    def evaluate(
        self,
        tool: str,
        payload: dict[str, Any],
        is_explicit_user_request: bool = True,
    ) -> tuple[PermissionLevel, bool]:
        """
        Return (PermissionLevel, requires_confirmation).

        Explicit user requests (e.g. "Send Ahmed a message on WhatsApp")
        do not require confirmation unless they are genuinely destructive or sensitive.
        """
        override = _PAYLOAD_AWARE_PERMISSIONS.get(tool)
        if override is not None:
            level = override(payload or {})
        else:
            level = _TOOL_PERMISSIONS.get(tool, PermissionLevel.NORMAL)

        if level == PermissionLevel.DESTRUCTIVE:
            return level, True

        if level == PermissionLevel.SENSITIVE and not is_explicit_user_request:
            return level, True

        # Normal user-requested messaging and application actions proceed directly
        return level, False


class ConfirmationStore:
    """SQLite-backed store for pending actions awaiting approval."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = str(db_path or MEMORY_DB_PATH)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.lock = threading.Lock()
        _ensure_permission_schema(self.conn)

    def create_pending_action(
        self,
        session_id: str,
        tool: str,
        payload: dict[str, Any],
        permission_level: PermissionLevel,
        task_id: str | None = None,
    ) -> str:
        """Persist a new pending action and return its action ID."""
        action_id = uuid.uuid4().hex
        now = _iso_now()
        payload_json = json.dumps(payload)

        with self.lock:
            self.conn.execute(
                """
                INSERT INTO pending_actions (id, session_id, task_id, tool, payload, permission_level, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (action_id, session_id, task_id, tool, payload_json, permission_level.value, ActionStatus.PENDING.value, now),
            )
            self.conn.commit()

        _log.info("Created pending action %s for tool %s", action_id, tool)
        return action_id

    def get_pending_action(self, action_id: str) -> dict[str, Any] | None:
        """Fetch a pending action by ID."""
        with self.lock:
            row = self.conn.execute(
                """
                SELECT id, session_id, task_id, tool, payload, permission_level, status, created_at, resolved_at, result_data
                FROM pending_actions WHERE id = ?
                """,
                (action_id,),
            ).fetchone()

            if not row:
                return None

            return {
                "id": row[0],
                "session_id": row[1],
                "task_id": row[2],
                "tool": row[3],
                "payload": json.loads(row[4]),
                "permission_level": row[5],
                "status": row[6],
                "created_at": row[7],
                "resolved_at": row[8],
                "result_data": json.loads(row[9]) if row[9] else {},
            }

    def list_pending_actions(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """List all currently pending actions."""
        with self.lock:
            if session_id:
                rows = self.conn.execute(
                    """
                    SELECT id, session_id, task_id, tool, payload, permission_level, status, created_at
                    FROM pending_actions WHERE status = 'pending' AND session_id = ?
                    ORDER BY created_at ASC
                    """,
                    (session_id,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """
                    SELECT id, session_id, task_id, tool, payload, permission_level, status, created_at
                    FROM pending_actions WHERE status = 'pending'
                    ORDER BY created_at ASC
                    """,
                ).fetchall()

            return [
                {
                    "id": r[0],
                    "session_id": r[1],
                    "task_id": r[2],
                    "tool": r[3],
                    "payload": json.loads(r[4]),
                    "permission_level": r[5],
                    "status": r[6],
                    "created_at": r[7],
                }
                for r in rows
            ]

    def approve_action(self, action_id: str, tool_runner=None) -> ToolResult:
        """Approve and execute a pending action."""
        action = self.get_pending_action(action_id)
        if not action:
            return ToolResult("permission", False, "Action not found", {"started": False, "completed": False})

        if action["status"] != ActionStatus.PENDING.value:
            return ToolResult("permission", False, f"Action is already {action['status']}", {"started": False, "completed": False})

        now = _iso_now()
        with self.lock:
            self.conn.execute(
                "UPDATE pending_actions SET status = ?, resolved_at = ? WHERE id = ?",
                (ActionStatus.APPROVED.value, now, action_id),
            )
            self.conn.commit()

        _log.info("Action %s approved. Executing tool %s", action_id, action["tool"])

        if tool_runner is None:
            result = ToolResult(
                action["tool"],
                False,
                "No tool runner available to execute the approved action",
                {"started": False, "completed": False},
            )
        else:
            from backend.interfaces import ToolCall
            call = ToolCall(action["tool"], action["payload"])
            try:
                # The runner's resume path must not re-trigger the gate.
                result = tool_runner.run(call, confirmed=True)
            except TypeError:
                result = tool_runner.run(call)

        status = ActionStatus.EXECUTED.value if result.success else ActionStatus.FAILED.value
        with self.lock:
            self.conn.execute(
                "UPDATE pending_actions SET status = ?, result_data = ? WHERE id = ?",
                (status, json.dumps(result.data or {}), action_id),
            )
            self.conn.commit()

        return result

    def reject_action(self, action_id: str) -> bool:
        """Reject/cancel a pending action."""
        action = self.get_pending_action(action_id)
        if not action or action["status"] != ActionStatus.PENDING.value:
            return False

        now = _iso_now()
        with self.lock:
            self.conn.execute(
                "UPDATE pending_actions SET status = ?, resolved_at = ? WHERE id = ?",
                (ActionStatus.REJECTED.value, now, action_id),
            )
            self.conn.commit()

        _log.info("Action %s rejected", action_id)
        return True

    def close(self) -> None:
        """Close SQLite database connection."""
        with self.lock:
            self.conn.close()
