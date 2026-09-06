"""
backend/live_runtime.py

Explicit construction of the live Jarvis runtime for the backend.

The backend must not import run_voice_test.py: that script runs heavy
initialization at import time (model loading, audio device setup) and
can call os._exit() on startup validation failure. Instead, the
backend asks this module to build a runtime on demand, so failures
surface as a normal exception the server can report cleanly.
"""

from __future__ import annotations

from typing import Any, Callable

from backend.bus import (
    BackendBus,
    LoggingObserver,
    session_started,
)
from backend.lifecycle import Lifecycle
from backend.models import Session


class RuntimeUnavailableError(Exception):
    """Raised when the live runtime cannot be constructed."""


def build_live_runtime(
    *,
    session_id: str = "voice-session",
    bus: BackendBus | None = None,
    attach_logging: bool = False,
) -> dict[str, Any]:
    """
    Build the live runtime (real audio, tools, agents) explicitly.

    This performs no work at import time; every heavy import happens
    inside this function, so a missing dependency raises a clean
    RuntimeUnavailableError instead of poisoning the process.

    Returns a dict with the same keys as backend.runtime.assemble_backend,
    plus the extra agent handles the server needs:
    bus, session, lifecycle, audio, tool_runner, orchestrator, brain,
    planner, memory, router.
    """
    try:
        from backend.live_adapters import (
            LiveAudioProvider,
            LiveToolRunner,
            LiveOrchestrator,
        )
        from agents.brain import JarvisBrain
        from agents.planner import TaskPlanner
        from core.memory import Memory
        from core.router import CommandRouter
    except Exception as exc:
        raise RuntimeUnavailableError(
            f"live runtime dependencies unavailable: {exc}"
        ) from exc

    try:
        runtime_bus = bus if bus is not None else BackendBus()

        if attach_logging:
            runtime_bus.subscribe(LoggingObserver(verbose=False))

        session = Session(session_id)
        runtime_bus.publish(session_started(session))

        lifecycle = Lifecycle(runtime_bus, session)
        lifecycle.mark_started()

        audio = LiveAudioProvider(bus=runtime_bus, session_id=session.id)
        tool_runner = LiveToolRunner(bus=runtime_bus, session_id=session.id)
        orchestrator = LiveOrchestrator()

        brain = JarvisBrain()
        planner = TaskPlanner()
        memory = Memory()
        router = CommandRouter()

        return {
            "bus": runtime_bus,
            "session": session,
            "lifecycle": lifecycle,
            "audio": audio,
            "tool_runner": tool_runner,
            "orchestrator": orchestrator,
            "brain": brain,
            "planner": planner,
            "memory": memory,
            "router": router,
        }
    except RuntimeUnavailableError:
        raise
    except Exception as exc:
        raise RuntimeUnavailableError(
            f"live runtime construction failed: {exc}"
        ) from exc


# Convenience alias so callers can inject a fake builder in tests.
RuntimeBuilder = Callable[..., dict[str, Any]]