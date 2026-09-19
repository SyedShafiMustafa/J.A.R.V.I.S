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
        from core.permission import PermissionEngine, ConfirmationStore
        from core.scheduler import TaskScheduler
    except Exception as exc:
        raise RuntimeUnavailableError(
            f"live runtime dependencies unavailable: {exc}"
        ) from exc

    try:
        runtime_bus = bus if bus is not None else BackendBus()

        if attach_logging:
            runtime_bus.subscribe(LoggingObserver(verbose=False))

        # Initialize memory and restore/create conversation
        memory = Memory()
        if session_id == "voice-session":
            last_conversation = memory.get_last_conversation()
            if last_conversation:
                session_id = last_conversation["id"]
            else:
                session_id = memory.create_conversation()

        session = Session(session_id)
        session.conversation_id = session_id
        runtime_bus.publish(session_started(session))

        lifecycle = Lifecycle(runtime_bus, session)
        lifecycle.mark_started()

        permission_engine = PermissionEngine()
        confirmation_store = ConfirmationStore()

        audio = LiveAudioProvider(bus=runtime_bus, session_id=session.id)
        tool_runner = LiveToolRunner(
            bus=runtime_bus,
            session_id=session.id,
            permission_engine=permission_engine,
            confirmation_store=confirmation_store,
        )
        orchestrator = LiveOrchestrator()

        brain = JarvisBrain()
        planner = TaskPlanner()
        router = CommandRouter()

        scheduler = TaskScheduler()

        def _run_scheduled(name: str, payload: dict) -> Any:
            from backend.interfaces import ToolCall

            payload = payload or {}

            # A reminder speaks the stored text instead of running a tool.
            remind = payload.get("remind")
            if remind:
                try:
                    audio.speak(str(remind))
                    audio.wait()
                except Exception:
                    import logging
                    logging.getLogger("jarvis.scheduler").warning(
                        "reminder speech failed", exc_info=True
                    )
                return None

            tool = payload.get("tool") or name
            args = payload.get("payload") if isinstance(payload.get("payload"), dict) else {
                k: v for k, v in payload.items() if k != "tool"
            }
            return tool_runner.run(ToolCall(tool=tool, payload=args))

        scheduler.set_action_handler(_run_scheduled)
        scheduler.start()

        # Warm up models to avoid cold-loading delays during first conversation
        _warm_up_models(audio, brain)

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
            "permission_engine": permission_engine,
            "confirmation_store": confirmation_store,
            "scheduler": scheduler,
        }
    except RuntimeUnavailableError:
        raise
    except Exception as exc:
        raise RuntimeUnavailableError(
            f"live runtime construction failed: {exc}"
        ) from exc


def _warm_up_models(audio, brain):
    """Pre-load models so the microphone and first reply are ready fast.

    Whisper and Piper are warmed synchronously: they are pure local model
    loads, they gate microphone readiness (WakeWordDetector is constructed
    right after this), and doing them here removes their cost from the
    first real turn.

    The Ollama warm-up ("Hello" round-trip) runs in a daemon thread: it is
    only a keep-alive/first-load optimization for the brain, and blocking
    runtime construction on it delays wake-listener startup by seconds
    (worse when the model needs to load from disk on CPU).
    """
    import logging
    import tempfile
    import threading

    import numpy as np
    import soundfile as sf

    _log = logging.getLogger("jarvis.runtime")

    # Warm up Whisper (STT) with a real 16 kHz WAV so the first actual
    # transcription does not pay lazy per-process initialization cost.
    # (The previous transcribe("") no-op never exercised the decode path.)
    try:
        _log.info("Warming up Whisper STT...")
        silence = np.zeros(16000, dtype=np.float32)
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        try:
            sf.write(tmp.name, silence, 16000)
            audio.stt.transcribe(tmp.name)
        finally:
            tmp.close()
        _log.info("Whisper STT warm-up complete")
    except Exception:
        _log.warning("Whisper warm-up failed", exc_info=True)

    # Warm up Piper TTS
    try:
        _log.info("Warming up Piper TTS...")
        audio.tts.speak(" ")
        audio.tts.wait()
    except Exception:
        _log.warning("Piper warm-up failed", exc_info=True)

    # Warm up Ollama connection without blocking startup.
    def _warm_ollama():
        try:
            list(brain.stream([{"role": "user", "content": "Hello"}]))
            _log.info("Ollama warm-up complete")
        except Exception:
            _log.warning("Ollama warm-up failed; startup will continue lazily", exc_info=True)

    threading.Thread(target=_warm_ollama, name="jarvis-ollama-warmup", daemon=True).start()


# Convenience alias so callers can inject a fake builder in tests.
RuntimeBuilder = Callable[..., dict[str, Any]]