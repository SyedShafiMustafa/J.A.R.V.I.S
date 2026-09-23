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

import logging
import threading
from typing import Any

from backend.bus import (
    BackendEvent,
    BackendBus,
    wake_listening,
    audio_start,
    audio_stop,
    tool_started,
    tool_finished,
    tool_failed,
    user_interrupt,
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

_log = logging.getLogger("jarvis.backend.audio")


def _make_barge_in_model():
    """Factory for the barge-in wake-word scorer.

    Module-level seam so tests can substitute a fake scorer without
    loading the real openwakeword ONNX models.
    """
    from openwakeword.model import Model

    return Model(inference_framework="onnx")


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
        self._wake_thread = None
        self._wake_lock = threading.Lock()
        self._wake_error_reported = False
        self._session_id = session_id
        self._speaking = False
        self._barge_in_thread = None
        self._barge_in_stop = threading.Event()
        self._barge_in_stream = None
        self._barge_in_lock = threading.Lock()
        self._barge_in_session = 0

    def start_wake_word(self) -> None:
        from audio.wake_word import WakeWordDetector

        with self._wake_lock:
            if self._wake_thread is not None and self._wake_thread.is_alive():
                return
            detector = WakeWordDetector(
                on_detect=self._on_wake_detected,
                on_error=self._on_wake_error,
                on_started=lambda: self.bus.publish(
                    wake_listening(session_id=self._session_id)
                ),
            )
            self._wake_error_reported = False
            self._wake_detector = detector
            thread = threading.Thread(
                target=self._run_wake_detector,
                args=(detector,),
                name="jarvis-wake-listener",
                daemon=True,
            )
            self._wake_thread = thread
            thread.start()

    def stop_wake_word(self) -> None:
        with self._wake_lock:
            detector = self._wake_detector
            thread = self._wake_thread
        if detector is None:
            return
        detector.stop()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if thread is not None and thread.is_alive():
            raise RuntimeError("wake word listener did not stop within 2 seconds")
        with self._wake_lock:
            if self._wake_detector is detector:
                self._wake_detector = None
            if self._wake_thread is thread:
                self._wake_thread = None
        self.bus.publish(wake_listening(session_id=self._session_id, meta={"stopped": True}))

    def _run_wake_detector(self, detector) -> None:
        try:
            detector.start()
        except Exception as exc:
            _log.exception("wake word listener failed")
            with self._wake_lock:
                reported = getattr(self, "_wake_error_reported", False)
            if not reported:
                self._on_wake_error(exc)
        finally:
            with self._wake_lock:
                if self._wake_detector is detector:
                    self._wake_detector = None
                    self._wake_thread = None

    def record_audio(self, cancel_event=None) -> str:
        self.bus.publish(audio_start(session_id=self._session_id))
        try:
            path = self.recorder.record(cancel_event=cancel_event)
            return path
        finally:
            self.bus.publish(audio_stop(session_id=self._session_id))

    def transcribe(self, audio_path: str) -> str:
        return self.stt.transcribe(audio_path)

    def speak(self, text: str) -> None:
        self._stop_barge_in()
        with self._barge_in_lock:
            self._barge_in_session += 1
            session = self._barge_in_session
        self._speaking = True
        self._barge_in_stop.clear()
        # The barge-in mic starts only when TTS audio actually reaches the
        # speakers (via the on_playback_start hook), never during synthesis —
        # otherwise the listener hears silence/queue lag and can outlive or
        # preempt the utterance it is meant to guard.
        self.tts.on_playback_start = lambda: self._start_barge_in_listener(session)
        self.tts.speak(text)

    def _start_barge_in_listener(self, session: int) -> None:
        """Start the barge-in mic listener; called from the TTS worker thread
        the moment playback actually begins."""
        with self._barge_in_lock:
            stale = (
                self._barge_in_session != session
                or not self._speaking
                or self._barge_in_stop.is_set()
            )
            if stale:
                return
            thread = threading.Thread(
                target=self._barge_in_listener,
                args=(session,),
                name="jarvis-barge-in-listener",
                daemon=True,
            )
            self._barge_in_thread = thread
        thread.start()

    def wait(self) -> None:
        self._speaking = False
        self._stop_barge_in()
        self.tts.wait()
        self._speaking = False

    def stop_speaking(self) -> None:
        self._speaking = False
        self._stop_barge_in()
        self.tts.stop_speaking()

    def _stop_barge_in(self) -> None:
        with self._barge_in_lock:
            self._barge_in_session += 1
            thread = self._barge_in_thread
            stream = self._barge_in_stream
            self._barge_in_stop.set()
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        if thread is threading.current_thread():
            return
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        if thread is not None and thread.is_alive():
            # A slow PortAudio stop must not fail the whole speaking turn;
            # the listener is session-invalidated and will exit on its own.
            _log.warning("barge-in listener slow to stop; continuing")
        with self._barge_in_lock:
            if self._barge_in_thread is thread:
                self._barge_in_thread = None
            if self._barge_in_stream is stream:
                self._barge_in_stream = None

    def _barge_in_listener(self, session: int) -> None:
        """Listen for the wake phrase while TTS is playing and interrupt it.

        Uses wake-word detection instead of raw loudness: an energy trigger
        hears the assistant's own voice through the speakers and
        self-interrupts every reply. The wake phrase is an intentional
        user gesture that the TTS audio itself never matches.
        """
        import sounddevice as sd
        import numpy as np
        import queue

        from config.config import SAMPLE_RATE, WAKEWORD

        q = queue.Queue()

        def callback(indata, frames, time, status):
            q.put(indata.copy())

        try:
            model = _make_barge_in_model()
        except Exception as exc:
            _log.warning("barge-in wake model unavailable; interrupt disabled for this turn: %s", exc)
            return

        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=1280,
            callback=callback,
        )
        with self._barge_in_lock:
            self._barge_in_stream = stream
        try:
            stream.start()
            while self._speaking and not self._barge_in_stop.is_set():
                try:
                    data = q.get(timeout=0.1)
                except queue.Empty:
                    continue

                audio = (data[:, 0] * 32767).astype(np.int16)
                try:
                    score = model.predict(audio).get(WAKEWORD, 0.0)
                except Exception:
                    continue

                if score > 0.5:
                    with self._barge_in_lock:
                        active = self._speaking and self._barge_in_session == session
                    if active:
                        self._speaking = False
                        self._barge_in_stop.set()
                        self.bus.publish(user_interrupt(session_id=self._session_id))
                        self.tts.stop_speaking()
                    break
        finally:
            try:
                stream.stop()
            finally:
                stream.close()
            with self._barge_in_lock:
                if self._barge_in_stream is stream:
                    self._barge_in_stream = None

    def _on_wake_detected(self) -> None:
        # Emit wake event so the voice loop can start a conversation
        from backend.bus import wake_detected
        self.bus.publish(wake_detected(session_id=self._session_id))

    def _on_wake_error(self, error: Exception) -> None:
        with self._wake_lock:
            self._wake_error_reported = True
        self.bus.publish(BackendEvent(
            kind="wake.error",
            session_id=self._session_id,
            meta={"error": str(error)},
        ))


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

    def __init__(
        self,
        bus: BackendBus,
        session_id: str | None = None,
        permission_engine: Any | None = None,
        confirmation_store: Any | None = None,
    ) -> None:
        from tools.executor import TaskExecutor

        self.bus = bus
        self._executor = TaskExecutor()
        self._session_id = session_id
        self._permission_engine = permission_engine
        self._confirmation_store = confirmation_store
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

    def run(
        self,
        call: ToolCall,
        task: Task | None = None,
        confirmed: bool = False,
    ) -> ToolResult:
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

        # Centralized permission gate: sensitive/destructive actions are not
        # executed inline. They are persisted as a pending action and resumed
        # verbatim when the user approves. `confirmed=True` is the resume path
        # used by the confirmation store, which must not re-block.
        if not confirmed:
            blocked = self._permission_gate(call, task_id=task_id)
            if blocked is not None:
                return blocked

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
                result = self._executor.execute(plan)
                if not result.success:
                    self.bus.publish(tool_failed(
                        session_id=self._session_id,
                        task_id=task_id,
                        tool=call.tool,
                        error=result.message,
                    ))
                    return ToolResult(
                        tool=call.tool,
                        success=False,
                        message=result.message,
                        data={"started": True, "completed": False, **(result.data or {})},
                    )
                self.bus.publish(tool_finished(
                    session_id=self._session_id,
                    task_id=task_id,
                    tool=call.tool,
                    success=True,
                ))
                return ToolResult(
                    tool=call.tool,
                    success=True,
                    message=result.message,
                    data={"started": True, "completed": True, **(result.data or {})},
                )
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

    def _permission_gate(self, call: ToolCall, task_id: str | None = None) -> ToolResult | None:
        """Return a blocking ToolResult when the action needs confirmation."""
        engine = self._permission_engine
        if engine is None:
            return None
        try:
            level, requires = engine.evaluate(
                call.tool, dict(call.payload), is_explicit_user_request=True
            )
        except Exception:
            _log.exception("permission evaluation failed")
            return None
        if not requires:
            return None

        action_id = None
        store = self._confirmation_store
        if store is not None:
            try:
                action_id = store.create_pending_action(
                    session_id=self._session_id or "voice-session",
                    tool=call.tool,
                    payload=dict(call.payload),
                    permission_level=level,
                    task_id=task_id,
                )
            except Exception:
                _log.exception("could not persist pending action")
                return ToolResult(
                    tool=call.tool,
                    success=False,
                    message="I couldn't safely record that action for confirmation.",
                    data={"started": False, "completed": False, "verified": False},
                )

        self.bus.publish(tool_failed(
            session_id=self._session_id,
            task_id=task_id,
            tool=call.tool,
            error="confirmation required",
        ))
        return ToolResult(
            tool=call.tool,
            success=False,
            message="That action is sensitive, so I need your confirmation first.",
            data={
                "confirmation_required": True,
                "action_id": action_id,
                "started": False,
                "completed": False,
                "verified": False,
            },
        )

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
            self._record_router_experience(user_text, reply)
            return OrchestratorDecision(
                kind="reply",
                reply=reply,
                metadata={"handled_by": "router"},
            )

        if _is_action_request(user_lower):
            # Surface bounded, relevant prior execution knowledge to the planner.
            experiences = self.memory.retrieve_experiences(user_text, limit=3)
            return OrchestratorDecision(
                kind="action",
                intent=user_text,
                metadata={
                    "handled_by": "planner",
                    "experiences": experiences,
                    "context": context,
                },
            )

        memories = self.memory.search_memories(user_text)
        if memories:
            context["memories"] = memories

        return OrchestratorDecision(
            kind="chat",
            intent=user_text,
            metadata={"handled_by": "brain", "context": context},
        )

    def _record_router_experience(self, user_text: str, reply: str) -> None:
        """Record the outcome of a router-handled (fast-path) app command.

        The command router resolves simple ``open``/``close`` requests without
        going through the planner, so without this the experience table would
        never learn which app names actually resolve on this machine.
        """
        text = user_text.lower()
        if any(w in text for w in ("close", "quit")):
            tool = "close_app"
        elif any(text.startswith(k) for k in ("open", "launch", "start", "run", "bring up", "use")):
            tool = "open_app"
        else:
            return
        low = reply.lower()
        outcome = "failure" if ("couldn't" in low or "cannot" in low or "unable" in low) else "success"
        try:
            self.memory.save_experience(
                scenario=f"tool:{tool}",
                strategy=user_text[:300],
                outcome=outcome,
                lesson=reply[:300],
            )
        except Exception:
            pass

    def plan_action(self, user_text: str, lessons: list[str] | None = None) -> Task:
        """Plan an action request into a tracked Task."""
        plan = self.planner.create_plan(user_text, lessons=lessons)
        task = Task(
            id=plan.get("goal", "action").replace(" ", "_")[:64],
            goal=plan.get("goal", user_text),
            steps=plan.get("steps", []),
        )
        return task


def _is_action_request(text: str) -> bool:
    """Single source of truth for action routing.

    Delegates to the global action-vs-explanation model
    (``core.intent``): imperative requests ACT (including polite forms
    like "Can you open Chrome?"), explicit how-to questions EXPLAIN
    (and therefore never reach the planner), everything else chats.
    """
    from core.intent import classify
    return classify(text) == "act"