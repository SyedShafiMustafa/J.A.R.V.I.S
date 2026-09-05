"""
backend/models.py

Compact session/task model for the backend.

This is the in-memory backbone for a conversation/session so state
does not leak through globals or ad hoc module variables.

A Session owns:
- identity / label
- current user utterance
- last decision from the orchestrator
- active task/plan while a tool sequence is running
- recent assistant reply
- cancellation flag
- simple lifecycle timestamp helpers

A Task represents an in-progress action plan and its runtime status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """One in-progress action plan produced by the planner."""

    id: str
    goal: str
    steps: list[dict[str, Any]]
    status: TaskStatus = field(default=TaskStatus.PENDING)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def start(self) -> None:
        self.status = TaskStatus.RUNNING
        self.started_at = datetime.now(timezone.utc)

    def complete(self, message: str = "ok") -> None:
        self.status = TaskStatus.COMPLETED
        self.result_message = message
        self.finished_at = datetime.now(timezone.utc)

    def fail(self, message: str) -> None:
        self.status = TaskStatus.FAILED
        self.result_message = message
        self.finished_at = datetime.now(timezone.utc)

    def cancel(self) -> None:
        self.status = TaskStatus.CANCELLED
        self.finished_at = datetime.now(timezone.utc)


@dataclass
class Session:
    """Lightweight session context for one wake-driven conversation."""

    id: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_user_text: str | None = None
    last_decision: dict[str, Any] | None = None
    active_task: Task | None = None
    last_assistant_reply: str | None = None
    cancelled: bool = False

    # Simple convenience helpers

    def note_user(self, text: str) -> None:
        self.last_user_text = text

    def note_decision(self, decision: dict[str, Any]) -> None:
        self.last_decision = decision

    def note_reply(self, reply: str) -> None:
        self.last_assistant_reply = reply

    def active_task_is_running(self) -> bool:
        return self.active_task is not None and self.active_task.status == TaskStatus.RUNNING

    def cancel_active_task(self) -> None:
        if self.active_task is not None:
            self.active_task.cancel()
        self.cancelled = True

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_at": self.started_at.isoformat(),
            "last_user_text": self.last_user_text,
            "active_task": None if self.active_task is None else {
                "id": self.active_task.id,
                "goal": self.active_task.goal,
                "status": self.active_task.status.value,
                "result_message": self.active_task.result_message,
            },
            "cancelled": self.cancelled,
        }
