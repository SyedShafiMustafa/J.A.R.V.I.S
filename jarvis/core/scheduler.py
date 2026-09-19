"""
core/scheduler.py

Background Task Scheduler for J.A.R.V.I.S.

Supports:
- One-time delayed tasks
- Recurring interval tasks
- Reminders
- Task persistence in SQLite across restarts
- Non-blocking execution, logging, and cancellation
"""

from __future__ import annotations

import json
import re
import time
import sqlite3
import threading
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any

from config.settings import MEMORY_DB_PATH
from backend.interfaces import ToolResult

_log = logging.getLogger("jarvis.scheduler")

# ---------------------------------------------------------------------------
# Natural-language scheduling intents
# ---------------------------------------------------------------------------

_TIME_UNITS = {
    "second": 1, "sec": 1, "s": 1,
    "minute": 60, "min": 60, "m": 60,
    "hour": 3600, "hr": 3600, "h": 3600,
    "day": 86400, "morning": 86400, "evening": 86400, "night": 86400,
}

# Phrases that signal "do this on a schedule" / "stop the schedule".
_CANCEL_WORDS = ("reminder", "reminders", "remind", "task", "tasks", "schedule", "scheduled")
_ACTION_HINTS = ("remind", "run", "do", "check", "send", "water", "call", "email")


def _unit_seconds(unit: str) -> int | None:
    return _TIME_UNITS.get((unit or "").lower())


def _extract_action(text: str) -> str:
    """The thing to remind the user about, from the trailing 'to <action>'."""
    m = re.search(r"\bto\s+(.+)$", text, re.IGNORECASE)
    if not m:
        return ""
    return m.group(1).strip().strip(".!?").strip()


def parse_schedule_request(text: str) -> dict[str, Any] | None:
    """Parse a reminder / recurring reminder / cancel request.

    Supported shapes (deliberately small and testable):
      - "remind me in 10 minutes to call mom"   -> one-time
      - "remind me to stretch in 30 seconds"     -> one-time
      - "remind me every 5 minutes to drink water" -> recurring
      - "every morning remind me to check mail"  -> recurring
      - "cancel my reminders"                    -> cancel

    Returns None when the text is not a scheduling request.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    low = raw.lower()

    if "cancel" in low and any(w in low for w in _CANCEL_WORDS):
        return {"kind": "cancel", "raw": raw}

    # Recurring: "every [N] <unit>"
    m = re.search(
        r"every\s+(?:(\d+)\s*)?(second|sec|minute|min|hour|hr|day|morning|evening|night)s?\b",
        low,
    )
    if m and any(h in low for h in _ACTION_HINTS):
        count = int(m.group(1)) if m.group(1) else 1
        unit_seconds = _unit_seconds(m.group(2))
        action = _extract_action(raw)
        if unit_seconds and action:
            return {
                "kind": "recurring",
                "interval_seconds": count * unit_seconds,
                "action_text": action,
                "raw": raw,
            }

    # One-time: "in N <unit>"
    m = re.search(r"in\s+(\d+)\s*(second|sec|minute|min|hour|hr)s?\b", low)
    if m and "remind" in low:
        unit_seconds = _unit_seconds(m.group(2))
        action = _extract_action(raw)
        if unit_seconds and action:
            return {
                "kind": "remind",
                "delay_seconds": int(m.group(1)) * unit_seconds,
                "action_text": action,
                "raw": raw,
            }

    return None


def humanize_seconds(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f} seconds"
    if seconds < 3600:
        return f"{seconds / 60:.0f} minutes"
    if seconds < 86400:
        return f"{seconds / 3600:.0f} hours"
    return f"{seconds / 86400:.0f} days"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_scheduler_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id TEXT PRIMARY KEY,
            task_name TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            run_at TEXT NOT NULL,
            interval_seconds REAL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'scheduled',
            created_at TEXT NOT NULL,
            last_run_at TEXT,
            run_count INTEGER DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scheduled_run_at ON scheduled_tasks(run_at, status)"
    )
    conn.commit()


class TaskScheduler:
    """SQLite-backed persistent task scheduler."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = str(db_path or MEMORY_DB_PATH)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.lock = threading.Lock()
        _ensure_scheduler_schema(self.conn)

        self._running = False
        self._worker_thread = None
        self._action_handler: Callable[[str, dict], ToolResult] | None = None

    def set_action_handler(self, handler: Callable[[str, dict], ToolResult]) -> None:
        self._action_handler = handler

    def schedule_task(
        self,
        task_name: str,
        payload: dict[str, Any],
        delay_seconds: float = 0.0,
        interval_seconds: float = 0.0,
        prevent_duplicates: bool = True,
    ) -> str:
        """Schedule a new task (one-time or recurring).

        Duplicate prevention: an identical pending task (same name + payload)
        returns the existing id instead of scheduling a second copy, so a
        repeated request never executes twice.
        """
        task_id = uuid.uuid4().hex
        now_ts = time.time() + delay_seconds
        run_at = datetime.fromtimestamp(now_ts, timezone.utc).isoformat()
        created_at = _iso_now()
        payload_json = json.dumps(payload)

        with self.lock:
            if prevent_duplicates:
                existing = self.conn.execute(
                    "SELECT id FROM scheduled_tasks "
                    "WHERE status = 'scheduled' AND task_name = ? AND payload_json = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (task_name, payload_json),
                ).fetchone()
                if existing:
                    _log.info("Duplicate scheduled task %s reused %s", task_name, existing[0])
                    return existing[0]
            self.conn.execute(
                """
                INSERT INTO scheduled_tasks (id, task_name, payload_json, run_at, interval_seconds, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'scheduled', ?)
                """,
                (task_id, task_name, payload_json, run_at, interval_seconds, created_at),
            )
            self.conn.commit()

        _log.info("Scheduled task %s (%s) for %s", task_id, task_name, run_at)
        return task_id

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a scheduled task."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE scheduled_tasks SET status = 'cancelled' WHERE id = ? AND status = 'scheduled'",
                (task_id,),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def list_scheduled_tasks(self) -> list[dict[str, Any]]:
        """List active scheduled tasks."""
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT id, task_name, payload_json, run_at, interval_seconds, status, created_at, run_count
                FROM scheduled_tasks WHERE status = 'scheduled' ORDER BY run_at ASC
                """
            ).fetchall()
            return [
                {
                    "id": r[0],
                    "task_name": r[1],
                    "payload": json.loads(r[2]),
                    "run_at": r[3],
                    "interval_seconds": r[4],
                    "status": r[5],
                    "created_at": r[6],
                    "run_count": r[7],
                }
                for r in rows
            ]

    def start(self) -> None:
        """Start background scheduler loop."""
        if self._running:
            return
        self._running = True
        thread = threading.Thread(target=self._scheduler_loop, name="jarvis-scheduler", daemon=True)
        self._worker_thread = thread
        thread.start()
        _log.info("Task scheduler started")

    def stop(self) -> None:
        """Stop background scheduler loop."""
        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)
        _log.info("Task scheduler stopped")

    def _scheduler_loop(self) -> None:
        while self._running:
            now_iso = _iso_now()
            due_tasks = []

            with self.lock:
                rows = self.conn.execute(
                    """
                    SELECT id, task_name, payload_json, interval_seconds, run_count
                    FROM scheduled_tasks
                    WHERE status = 'scheduled' AND run_at <= ?
                    """,
                    (now_iso,),
                ).fetchall()
                for r in rows:
                    due_tasks.append({
                        "id": r[0],
                        "task_name": r[1],
                        "payload": json.loads(r[2]),
                        "interval_seconds": r[3],
                        "run_count": r[4],
                    })

            for t in due_tasks:
                self._execute_due_task(t)

            time.sleep(1.0)

    def _execute_due_task(self, task_data: dict[str, Any]) -> None:
        task_id = task_data["id"]
        task_name = task_data["task_name"]
        payload = task_data["payload"]
        interval = task_data["interval_seconds"]
        run_count = task_data["run_count"] + 1
        now_iso = _iso_now()

        _log.info("Executing scheduled task %s (%s)", task_id, task_name)

        if self._action_handler is not None:
            try:
                self._action_handler(task_name, payload)
            except Exception:
                _log.exception("Error executing scheduled task %s", task_id)

        with self.lock:
            if interval > 0:
                next_ts = time.time() + interval
                next_run = datetime.fromtimestamp(next_ts, timezone.utc).isoformat()
                self.conn.execute(
                    "UPDATE scheduled_tasks SET run_at = ?, last_run_at = ?, run_count = ? WHERE id = ?",
                    (next_run, now_iso, run_count, task_id),
                )
            else:
                self.conn.execute(
                    "UPDATE scheduled_tasks SET status = 'completed', last_run_at = ?, run_count = ? WHERE id = ?",
                    (now_iso, run_count, task_id),
                )
            self.conn.commit()

    def close(self) -> None:
        self.stop()
        with self.lock:
            self.conn.close()
