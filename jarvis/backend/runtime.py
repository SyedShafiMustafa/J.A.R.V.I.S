"""
backend/runtime.py

Backend assembly helper for wiring the runtime pieces together.

This is the single place that says how the backend is normally
assembled from its contracts, adapters, session, bus, lifecycle,
orchestrator, and tool runner.

Keeping this centralized makes the runtime easier to read and
makes it simpler to swap in fakes for tests later without
copy-pasting wiring logic across the codebase.
"""

from __future__ import annotations

from typing import Callable

from backend.bus import (
    BackendBus,
    LoggingObserver,
    ReplayObserver,
    session_started,
    BackendEvent,
)
from backend.lifecycle import Lifecycle, make_audio_cleanup
from backend.models import Session
from backend.interfaces import (
    AudioProvider,
    ToolRunner,
    Orchestrator,
    FakeAudioProvider,
    FakeToolRunner,
)


def assemble_backend(
    *,
    session_id: str = "default-session",
    audio: AudioProvider | None = None,
    tool_runner: ToolRunner | None = None,
    orchestrator: Orchestrator | None = None,
    bus: BackendBus | None = None,
    observers: list[Callable] | None = None,
    attach_logging: bool = True,
) -> dict:
    """
    Build and wire a standard backend runtime assembly.

    Returns a dict with the main runtime handles so callers can
    start the loop, subscribe extra observers, or shut down
    cleanly without re-deriving the wiring themselves.
    """
    if bus is None:
        bus = BackendBus()

    if attach_logging:
        bus.subscribe(LoggingObserver(verbose=True))

    if observers:
        for observer in observers:
            bus.subscribe(observer)

    session = Session(session_id)
    bus.publish(session_started(session))

    lifecycle = Lifecycle(bus, session)
    lifecycle.mark_started()

    live_audio = audio if audio is not None else FakeAudioProvider()
    live_runner = tool_runner if tool_runner is not None else FakeToolRunner()
    live_orchestrator = orchestrator if orchestrator is not None else FakeOrchestrator()

    return {
        "bus": bus,
        "session": session,
        "lifecycle": lifecycle,
        "audio": live_audio,
        "tool_runner": live_runner,
        "orchestrator": live_orchestrator,
    }


# Fake stubs used only when the caller does not supply real pieces.
# In a real wiring path, these would be swapped for LiveAudioProvider,
# LiveToolRunner, and LiveOrchestrator.

class FakeOrchestrator:
    """Minimal orchestrator placeholder for test/runtime assembly."""

    def decide(self, user_text: str, context=None):
        return OrchestratorDecision(kind="chat", intent=user_text, reply="assembly fake")



