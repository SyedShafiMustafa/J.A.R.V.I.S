"""
backend/bus.py

Compact internal event/observation bus for the backend.

It gives modules a single way to announce what is happening without
every module reaching directly into every other module.

Typical events:
- wake detected
- audio start/stop
- transcription ready
- tool started/finished/failed
- user interruption
- session/task lifecycle events

This is intentionally simple. It is synchronous, in-memory, and
meant for bookkeeping, debugging, and later hooks, not for a full
distributed trace system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from backend.models import Session, Task, TaskStatus

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BackendEvent:
    """One structured observation from the backend."""

    kind: str
    session_id: str | None = None
    task_id: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    meta: dict[str, Any] = field(default_factory=dict)

    def with_session(self, session: Session | None) -> BackendEvent:
        sid = None if session is None else session.id
        return BackendEvent(
            kind=self.kind,
            session_id=sid,
            task_id=self.task_id,
            timestamp=self.timestamp,
            meta=self.meta,
        )

    def with_task(self, task: Task | None) -> BackendEvent:
        tid = None if task is None else task.id
        return BackendEvent(
            kind=self.kind,
            session_id=self.session_id,
            task_id=tid,
            timestamp=self.timestamp,
            meta=self.meta,
        )


# ---------------------------------------------------------------------------
# Common event kinds
# ---------------------------------------------------------------------------

def wake_detected(session_id: str | None = None, meta: dict[str, Any] | None = None) -> BackendEvent:
    return BackendEvent(
        kind="wake.detected",
        session_id=session_id,
        meta=meta or {},
    )


def wake_listening(session_id: str | None = None, meta: dict[str, Any] | None = None) -> BackendEvent:
    return BackendEvent(kind="wake.listening", session_id=session_id, meta=meta or {})


def audio_start(session_id: str | None = None, meta: dict[str, Any] | None = None) -> BackendEvent:
    return BackendEvent(kind="audio.start", session_id=session_id, meta=meta or {})


def audio_stop(session_id: str | None = None, meta: dict[str, Any] | None = None) -> BackendEvent:
    return BackendEvent(kind="audio.stop", session_id=session_id, meta=meta or {})


def transcription_ready(
    session_id: str | None = None,
    user_text: str | None = None,
    meta: dict[str, Any] | None = None,
) -> BackendEvent:
    return BackendEvent(
        kind="stt.ready",
        session_id=session_id,
        meta={"user_text": user_text, **(meta or {})},
    )


def tool_started(
    session_id: str | None = None,
    task_id: str | None = None,
    tool: str | None = None,
    meta: dict[str, Any] | None = None,
) -> BackendEvent:
    return BackendEvent(
        kind="tool.started",
        session_id=session_id,
        task_id=task_id,
        meta={"tool": tool, **(meta or {})},
    )


def tool_finished(
    session_id: str | None = None,
    task_id: str | None = None,
    tool: str | None = None,
    success: bool | None = None,
    meta: dict[str, Any] | None = None,
) -> BackendEvent:
    return BackendEvent(
        kind="tool.finished",
        session_id=session_id,
        task_id=task_id,
        meta={"tool": tool, "success": success, **(meta or {})},
    )


def tool_failed(
    session_id: str | None = None,
    task_id: str | None = None,
    tool: str | None = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> BackendEvent:
    return BackendEvent(
        kind="tool.failed",
        session_id=session_id,
        task_id=task_id,
        meta={"tool": tool, "error": error, **(meta or {})},
    )


def user_interrupt(session_id: str | None = None, meta: dict[str, Any] | None = None) -> BackendEvent:
    return BackendEvent(kind="user.interrupt", session_id=session_id, meta=meta or {})


def session_started(session: Session) -> BackendEvent:
    return BackendEvent(
        kind="session.started",
        session_id=session.id,
        meta={"summary": session.summary()},
    )


def session_ended(session: Session) -> BackendEvent:
    return BackendEvent(
        kind="session.ended",
        session_id=session.id,
        meta={"summary": session.summary()},
    )


def task_started(task: Task, session_id: str | None = None) -> BackendEvent:
    return BackendEvent(
        kind="task.started",
        session_id=session_id,
        task_id=task.id,
        meta={"goal": task.goal, "steps": task.steps},
    )


def task_completed(task: Task, session_id: str | None = None) -> BackendEvent:
    return BackendEvent(
        kind="task.completed",
        session_id=session_id,
        task_id=task.id,
        meta={"result_message": task.result_message},
    )


def task_failed(task: Task, session_id: str | None = None) -> BackendEvent:
    return BackendEvent(
        kind="task.failed",
        session_id=session_id,
        task_id=task.id,
        meta={"result_message": task.result_message},
    )


def task_cancelled(task: Task, session_id: str | None = None) -> BackendEvent:
    return BackendEvent(
        kind="task.cancelled",
        session_id=session_id,
        task_id=task.id,
        meta={"result_message": task.result_message},
    )


# ---------------------------------------------------------------------------
# Listener type
# ---------------------------------------------------------------------------

BackendListener = Callable[[BackendEvent], None]


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------

class BackendBus:
    """In-memory publish/subscribe bus for backend observations."""

    def __init__(self) -> None:
        self._listeners: list[BackendListener] = []

    def subscribe(self, listener: BackendListener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener: BackendListener) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def publish(self, event: BackendEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                # Never let an observer break the backend flow.
                pass


# ---------------------------------------------------------------------------
# Built-in observers
# ---------------------------------------------------------------------------

class LoggingObserver:
    """Print structured backend events to stderr for debugging."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    def __call__(self, event: BackendEvent) -> None:
        from backend.logging import log_event

        log_event(
            f"bus.{event.kind.replace('_', '.')}",
            "backend.bus",
            {"session_id": event.session_id, "task_id": event.task_id, **event.meta}
            if self.verbose
            else {"session_id": event.session_id, "task_id": event.task_id},
        )


class ReplayObserver:
    """Store events so they can be inspected later in tests or analysis."""

    def __init__(self) -> None:
        self.events: list[BackendEvent] = []

    def __call__(self, event: BackendEvent) -> None:
        self.events.append(event)
