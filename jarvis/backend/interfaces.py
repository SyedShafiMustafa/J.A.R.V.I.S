"""
backend/interfaces.py

Clean internal API boundaries for the JARVIS backend.

These interfaces are the contract between layers:
- audio provider contract (wake, STT, TTS)
- tool protocol (input schema, output schema, error contract)
- orchestrator interface (receive intent, return action + metadata)

The goal is that each piece can be swapped, faked, or tested
without dragging the whole app along.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from backend.models import Session, Task, TaskStatus


# ============================================================================
# Shared error taxonomy
# ============================================================================

class BackendError(Exception):
    """Base class for backend-layer failures."""

    pass


class ConfigurationError(BackendError):
    """Missing or invalid configuration / dependencies."""

    pass


class TransientError(BackendError):
    """Temporary failure, may succeed on retry."""

    pass


class PermanentError(BackendError):
    """Non-retryable failure, bad input or unsupported operation."""

    pass


# ============================================================================
# Audio provider contract
# ============================================================================

@runtime_checkable
class AudioProvider(Protocol):
    """Wake word + STT + TTS, presented as one logical audio surface."""

    def start_wake_word(self) -> None:
        ...

    def stop_wake_word(self) -> None:
        ...

    def record_audio(self) -> str:
        """Return a path to a recorded audio file."""

    def transcribe(self, audio_path: str) -> str:
        """Return transcribed text."""

    def speak(self, text: str) -> None:
        ...

    def stop_speaking(self) -> None:
        ...


# ============================================================================
# Tool protocol
# ============================================================================

@dataclass(frozen=True)
class ToolCall:
    """One step requested by the planner / orchestrator."""

    tool: str
    payload: dict[str, Any]


@dataclass
class ToolResult:
    """Structured outcome from executing a tool."""

    tool: str
    success: bool
    message: str
    data: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


@runtime_checkable
class ToolRunner(Protocol):
    """Executes a tool call and returns a structured result."""

    def run(self, call: ToolCall, task: Task | None = None) -> ToolResult:
        ...


# ============================================================================
# Orchestrator interface
# ============================================================================

@dataclass
class OrchestratorDecision:
    """What the backend decided to do with a user utterance."""

    kind: str
    intent: str | None = None
    reply: str | None = None
    tool_calls: list[ToolCall] | None = None
    metadata: dict[str, Any] | None = None
    session: Session | None = None
    task: Task | None = None


@runtime_checkable
class Orchestrator(Protocol):
    """Take a user utterance and decide what the backend should do next."""

    def decide(self, user_text: str, context: dict[str, Any] | None = None) -> OrchestratorDecision:
        ...


# ============================================================================
# Convenience: simple fake implementations for testing
# ============================================================================

class FakeAudioProvider:
    """No-op audio provider useful for unit tests and offline runs."""

    def start_wake_word(self) -> None:
        pass

    def stop_wake_word(self) -> None:
        pass

    def record_audio(self) -> str:
        return ""

    def transcribe(self, audio_path: str) -> str:
        return ""

    def speak(self, text: str) -> None:
        pass

    def stop_speaking(self) -> None:
        pass


class FakeToolRunner:
    """Tool runner that records calls and returns success by default.

    Its ``run()`` signature matches the real tool runner contract so
    fake-based tests stay aligned with the live adapter.
    """

    def __init__(self):
        self.calls: list[ToolCall] = []

    def run(
        self,
        call: ToolCall,
        task: Task | None = None,
    ) -> ToolResult:
        self.calls.append(call)
        return ToolResult(
            tool=call.tool,
            success=True,
            message=f"fake:{call.tool}",
            data=dict(call.payload),
        )
