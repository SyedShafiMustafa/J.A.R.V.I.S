"""
backend/lifecycle.py

Backend startup/shutdown lifecycle and cancellation handling.

The goal is to make the backend able to shut itself down cleanly
instead of only relying on abrupt exits.

It provides:
- a Lifecycle object that owns startup/shutdown state
- explicit cleanup hooks for audio, wake word, TTS, and other
  runtime resources
- a cancellation signal that other modules can check
- a simple shutdown() entry point that should be called on exit

This is intentionally lightweight. It is meant to be extended
with real device/thread/subprocess cleanup later without changing
the rest of the backend contracts.
"""

from __future__ import annotations

import threading
from typing import Callable

from backend.bus import (
    BackendBus,
    BackendEvent,
    session_ended,
    wake_listening,
    audio_stop,
)
from backend.logging import log_info, log_error
from backend.models import Session


class Lifecycle:
    """
    Simple runtime lifecycle container for the backend.

    It tracks:
    - whether the backend is running
    - whether shutdown has been requested
    - a set of cleanup callbacks to run on exit
    """

    def __init__(self, bus: BackendBus, session: Session) -> None:
        self._bus = bus
        self._session = session
        self._running = False
        self._shutdown_requested = False
        self._lock = threading.Lock()
        self._cleanup_callbacks: list[Callable[[], None]] = []

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def shutdown_requested(self) -> bool:
        with self._lock:
            return self._shutdown_requested

    def mark_started(self) -> None:
        with self._lock:
            self._running = True

    def request_shutdown(self) -> None:
        with self._lock:
            if not self._shutdown_requested:
                self._shutdown_requested = True
                self._session.cancel_active_task()

    def register_cleanup(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if callback not in self._cleanup_callbacks:
                self._cleanup_callbacks.append(callback)

    def shutdown(self) -> None:
        """
        Run cleanup callbacks and announce session end.

        This is the main backend shutdown entry point. It should be
        called when the app is exiting normally or when the user
        explicitly asks to stop.
        """
        with self._lock:
            if not self._running:
                return

            self._running = False
            self._shutdown_requested = True
            self._session.cancel_active_task()

        self._run_cleanups()
        self._bus.publish(session_ended(self._session))
        log_info("backend.lifecycle", "shutdown complete")

    def _run_cleanups(self) -> None:
        with self._lock:
            callbacks = list(self._cleanup_callbacks)

        for callback in callbacks:
            try:
                callback()
            except Exception as e:
                log_error("backend.lifecycle", "cleanup callback failed", {"error": str(e)})


# ---------------------------------------------------------------------------
# Convenience helpers for audio-aware lifecycle cleanup
# ---------------------------------------------------------------------------

def make_audio_cleanup(
    bus: BackendBus,
    session_id: str | None,
    stop_wake_word: Callable[[], None],
    stop_speaking: Callable[[], None],
) -> Callable[[], None]:
    """
    Build a cleanup callback that stops wake word, speaking, and
    emits basic audio stop events.

    This is a small helper so the runtime adapters can register
    proper shutdown behavior without duplicating the logic.
    """

    def cleanup() -> None:
        stop_wake_word()
        stop_speaking()
        bus.publish(audio_stop(session_id=session_id))

    return cleanup
