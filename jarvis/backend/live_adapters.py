"""
backend/live_adapters.py

Live adapters that present the existing runtime pieces
(audio, tools, agents) through the backend contracts.

These were previously defined inside run_voice_test.py, which meant
importing that script ran heavy initialization. Moving the adapters
here lets the backend construct the runtime explicitly, lazily, and
without import-time side effects.

Design rules:
- no module-level side effects
- all heavyweight imports happen inside constructors/methods so the
  module itself imports cleanly even when optional dependencies are
  missing (missing deps surface as a clean runtime-build failure)
- tool calls are validated against the registry before execution
- retries only apply to idempotent tools
"""

from __future__ import annotations

from typing import Any

from backend.bus import (
    BackendBus,
    wake_listening,
    audio_start,
    audio_stop,
    tool_started,
    tool_finished,
    tool_failed,
)
from backend.interfaces import (
    OrchestratorDecision,
    ToolCall,
    ToolResult,
    TransientError,
)
from backend.models import Task
from backend.retry import retry, RetryConfig
from backend.tools import (
    ToolDefinition,
    ToolError,
    build_default_tool_registry,
)


# ---------------------------------------------------------------------------
# ADAPTER: existing audio stack -> AudioProvider
# ---------------------------------------------------------------------------

class LiveAudioProvider:
    """
    Adapter that presents the current audio modules as a single
    AudioProvider contract.

    This is where audio wiring lives, so the rest of the backend
    doesn't care whether wake word, STT, and TTS are local, remote,
    or faked for tests.
    """

    def __init__(self, bus: BackendBus, session_id: str | None = None) -> None:
        from audio.vad import VoiceRecorder
        from audio.stt import SpeechToText
        from audio.tts import TextToSpeech

        self.bus = bus
        self.recorder = VoiceRecorder()
        self.stt = SpeechToText()
        self.tts = TextToSpeech()
        self._wake_detector = None
        self._session_id = session_id

    def start_wake_word(self) -> None:
        if self._wake_detector is not None:
            raise RuntimeError("Wake word detector already running")

        from audio.wake_word import WakeWordDetector

        self._wake_detector = WakeWordDetector(on_detect=self._on_wake_detected)
        self._wake_detector.start()

        self.bus.publish(wake_listening(session_id=self._session_id))

    def stop_wake_word(self) -> None:
        if self._wake_detector is None:
            return

        self._wake_detector = None

        self.bus.publish(wake_listening(session_id=self._session_id, meta={"stopped": True}))

    def record_audio(self) -> str:
        self.bus.publish(audio_start(session_id=self._session_id))
        try:
            path = self.recorder.record()
            return path
        finally:
            self.bus.publish(audio_stop(session_id=self._session_id))

    def transcribe(self, audio_path: str) -> str:
        return self.stt.transcribe(audio_path)

    def speak(self, text: str) -> None:
        self.tts.speak(text)

    def wait(self) -> None:
        self.tts.wait()

    def stop_speaking(self) -> None:
        self.tts.stop()

    def _on_wake_detected(self) -> None:
        raise NotImplementedError(
            "LiveAudioProvider does not run the conversation directly. "
            "Wake detection should hand off to the orchestrator layer."
        )


# ---------------------------------------------------------------------------
# ADAPTER: existing tools -> ToolRunner
# ---------------------------------------------------------------------------

class LiveToolRunner:
    """
    Adapter that executes ToolCall objects using the existing
    desktop/computer/vision tool stack.

    This gives tools a stable input/output contract and a single
    place for validation, retries, timeouts, and structured logging.
    """

    def __init__(self, bus: BackendBus, session_id: str | None = None) -> None:
        from tools.executor import TaskExecutor

        self.bus = bus
        self._executor = TaskExecutor()
        self._session_id = session_id
        self._retry_config = RetryConfig(
            max_attempts=3,
            base_delay_s=0.5,
            backoff=2.0,
            max_delay_s=5.0,
            jitter=True,
        )
        self._registry = build_default_tool_registry()

    def tool_definition(self, tool: str) -> ToolDefinition | None:
        return self._registry.get(tool)

    def run(self, call: ToolCall, task: Task | None = None) -> ToolResult:
        task_id = None if task is None else task.id

        definition = self._registry.get(call.tool)
        if definition is None:
            self.bus.publish(tool_failed(
                session_id=self._session_id,
                task_id=task_id,
                tool=call.tool,
                error=f"Unknown tool: {call.tool}",
            ))
            return ToolError(
                tool=call.tool,
                reason=f"Unknown tool: {call.tool}",
            ).to_result()

        def _execute() -> ToolResult:
            self.bus.publish(tool_started(
                session_id=self._session_id,
                task_id=task_id,
                tool=call.tool,
            ))

            try:
                step = dict(call.payload)
                step["tool"] = call.tool
                plan = {"goal": call.tool, "steps": [step]}
                success = self._executor.execute(plan)
                result = ToolResult(
                    tool=call.tool,
                    success=success,
                    message="ok" if success else "tool reported failure",
                )
                self.bus.publish(tool_finished(
                    session_id=self._session_id,
                    task_id=task_id,
                    tool=call.tool,
                    success=success,
                ))
                return result
            except TransientError:
                self.bus.publish(tool_failed(
                    session_id=self._session_id,
                    task_id=task_id,
                    tool=call.tool,
                    error="transient",
                ))
                raise
            except Exception as e:
                self.bus.publish(tool_failed(
                    session_id=self._session_id,
                    task_id=task_id,
                    tool=call.tool,
                    error=str(e),
                ))
                raise TransientError(f"Tool execution failed: {e}") from e

        # Retrying a non-idempotent tool (typing, clicking, hotkeys,
        # sending messages) can duplicate side effects. Only retry
        # tools that are explicitly marked idempotent.
        if definition.idempotent:
            return retry(_execute, config=self._retry_config)

        return _execute()

    def dry_run(self, call: ToolCall) -> ToolResult:
        """Validate a tool call and describe what would happen without running it."""
        definition = self._registry.get(call.tool)

        if definition is None:
            return ToolError(
                tool=call.tool,
                reason=f"Unknown tool: {call.tool}",
            ).to_result()

        if not definition.supports_dry_run:
            return ToolError(
                tool=call.tool,
                reason=f"Dry run is not supported for tool: {call.tool}",
            ).to_result()

        missing: list[str] = []

        for field in definition.input_fields:
            if field.get("required") and field["name"] not in call.payload:
                missing.append(field["name"])

        if missing:
            return ToolError(
                tool=call.tool,
                reason=f"Missing required fields: {', '.join(missing)}",
                detail={"missing": missing},
            ).to_result()

        return ToolResult(
            tool=call.tool,
            success=True,
            message=f"Would execute {call.tool}",
            data={"definition": definition.describe(), "payload": dict(call.payload)},
        )


# ---------------------------------------------------------------------------
# ADAPTER: existing router/brain/planner -> Orchestrator
# ---------------------------------------------------------------------------

class LiveOrchestrator:
    """
    Orchestrator that decides what to do with a user utterance.

    It preserves the existing behavior:
    - simple commands go through the command router
    - action requests go through planner + executor
    - everything else goes to the brain
    """

    def __init__(self) -> None:
        from core.router import CommandRouter
        from agents.planner import TaskPlanner
        from agents.brain import JarvisBrain
        from core.memory import Memory

        self.router = CommandRouter()
        self.planner = TaskPlanner()
        self.brain = JarvisBrain()
        self.memory = Memory()

    def decide(self, user_text: str, context: dict[str, Any] | None = None) -> OrchestratorDecision:
        context = context or {}
        user_lower = user_text.lower()

        handled, reply = self.router.route(user_text)
        if handled:
            return OrchestratorDecision(
                kind="reply",
                reply=reply,
                metadata={"handled_by": "router"},
            )

        if _is_action_request(user_lower):
            return OrchestratorDecision(
                kind="action",
                intent=user_text,
                metadata={"handled_by": "planner"},
            )

        memories = self.memory.search_memories(user_text)
        if memories:
            context["memories"] = memories

        return OrchestratorDecision(
            kind="chat",
            intent=user_text,
            metadata={"handled_by": "brain", "context": context},
        )

    def plan_action(self, user_text: str) -> Task:
        """Plan an action request into a tracked Task."""
        plan = self.planner.create_plan(user_text)
        task = Task(
            id=plan.get("goal", "action").replace(" ", "_")[:64],
            goal=plan.get("goal", user_text),
            steps=plan.get("steps", []),
        )
        return task


def _is_action_request(text: str) -> bool:
    action_words = [
        "open",
        "launch",
        "start",
        "close",
        "search",
        "find",
        "play",
        "click",
        "type",
        "write",
        "message",
        "send",
        "scroll",
        "press",
        "youtube",
        "google",
    ]
    return any(word in text for word in action_words)