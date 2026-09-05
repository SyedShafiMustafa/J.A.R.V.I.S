"""
backend/logging.py

Lightweight structured logging for the backend.

The goal is not to build a full observability platform here.
It is to make the important backend boundaries emit consistent,
searchable events with enough context to debug later.

Events are simple dictionaries with:
- event: a short event name
- at: where it happened
- meta: optional extra context

Those can be printed, logged, or later wired to a bus/file/trace
backend without changing the call sites much.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event: str, at: str, meta: dict[str, Any] | None = None) -> None:
    """Emit a structured backend event to stderr by default."""

    payload: dict[str, Any] = {
        "ts": now_iso(),
        "event": event,
        "at": at,
    }

    if meta:
        payload["meta"] = meta

    line = _format_event(payload)
    print(line, file=sys.stderr)


def log_enter(at: str, meta: dict[str, Any] | None = None) -> None:
    log_event("backend.enter", at, meta)


def log_exit(at: str, meta: dict[str, Any] | None = None) -> None:
    log_event("backend.exit", at, meta)


def log_error(at: str, message: str, meta: dict[str, Any] | None = None) -> None:
    log_event("backend.error", at, {"message": message, **(meta or {})})


def log_info(at: str, message: str, meta: dict[str, Any] | None = None) -> None:
    log_event("backend.info", at, {"message": message, **(meta or {})})


def log_timing(at: str, elapsed_s: float, meta: dict[str, Any] | None = None) -> None:
    log_event("backend.timing", at, {"elapsed_s": elapsed_s, **(meta or {})})


def _format_event(payload: dict[str, Any]) -> str:
    """Format a structured event as a single readable line."""

    event = payload.get("event", "unknown")
    ts = payload.get("ts", "")
    at = payload.get("at", "")

    parts: list[str] = [f"[{ts}]", event, f"@{at}"]

    for key in ("message",):
        if key in payload:
            parts.append(f"{key}={payload[key]!r}")

    for key, value in sorted(payload.get("meta", {}).items()):
        if key == "message":
            continue
        parts.append(f"{key}={value!r}")

    return " ".join(parts)
