import os
import re
import sys
import time
import random
from pathlib import Path

# Make imports work no matter which directory this script is run from
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.validation import validate_backend_startup
from backend.logging import log_info, log_error
from backend.bus import (
    BackendBus,
    wake_detected,
    wake_listening,
    audio_start,
    audio_stop,
    transcription_ready,
    tool_started,
    tool_finished,
    tool_failed,
    user_interrupt,
    session_started,
    task_started,
    task_completed,
    task_failed,
    LoggingObserver,
)
from typing import Callable

from backend.bus import (
    BackendBus,
    wake_detected,
    wake_listening,
    audio_start,
    audio_stop,
    transcription_ready,
    tool_started,
    tool_finished,
    tool_failed,
    user_interrupt,
    session_started,
    task_started,
    task_completed,
    task_failed,
    LoggingObserver,
)
from backend.lifecycle import Lifecycle, make_audio_cleanup
from backend.retry import retry, RetryConfig
from backend.tools import (
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolRegistry,
    build_default_tool_registry,
)

from audio.wake_word import WakeWordDetector
from audio.tts import TextToSpeech

from agents.brain import JarvisBrain
from agents.planner import TaskPlanner

from core.memory import Memory
from core.router import CommandRouter

from backend.interfaces import (
    AudioProvider,
    ToolCall,
    ToolResult,
    ToolRunner,
    Orchestrator,
    OrchestratorDecision,
)


# --------------------------------------------------
# ADAPTER: existing audio stack -> AudioProvider
# --------------------------------------------------

class LiveAudioProvider:
    """
    Adapter that presents the current audio modules as a single
    AudioProvider contract.

    This is where audio wiring lives, so the rest of the backend
    doesn't care whether wake word, STT, and TTS are local, remote,
    or faked for tests.
    """

    def __init__(self, session_id: str | None = None):
        from audio.vad import VoiceRecorder
        from audio.stt import SpeechToText

        self.recorder = VoiceRecorder()
        self.stt = SpeechToText()
        self.tts = TextToSpeech()
        self._wake_detector: WakeWordDetector | None = None
        self._session_id = session_id

    def start_wake_word(self) -> None:
        if self._wake_detector is not None:
            raise RuntimeError("Wake word detector already running")

        self._wake_detector = WakeWordDetector(on_detect=self._on_wake_detected)
        self._wake_detector.start()

        bus.publish(wake_listening(session_id=self._session_id))

    def stop_wake_word(self) -> None:
        if self._wake_detector is None:
            return

        self._wake_detector = None

        bus.publish(wake_listening(session_id=self._session_id, meta={"stopped": True}))

    def record_audio(self) -> str:
        bus.publish(audio_start(session_id=self._session_id))
        try:
            path = self.recorder.record()
            return path
        finally:
            bus.publish(audio_stop(session_id=self._session_id))

    def transcribe(self, audio_path: str) -> str:
        text = self.stt.transcribe(audio_path)
        return text

    def speak(self, text: str) -> None:
        self.tts.speak(text)

    def stop_speaking(self) -> None:
        self.tts.stop()

    # --------------------------------------------------
    # Wake word wiring kept close to audio, not spread
    # across the orchestration layer
    # --------------------------------------------------

    def _on_wake_detected(self) -> None:
        raise NotImplementedError(
            "LiveAudioProvider does not run the conversation directly. "
            "Wake detection should hand off to the orchestrator layer."
        )


# --------------------------------------------------
# ADAPTER: existing tools -> ToolRunner
# --------------------------------------------------

class LiveToolRunner:
    """
    Adapter that executes ToolCall objects using the existing
    desktop/computer/vision tool stack.

    This gives tools a stable input/output contract and a single
    place for retries, timeouts, and structured logging later.
    """

    def __init__(self, session_id: str | None = None):
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

        def _execute() -> ToolResult:
            bus.publish(tool_started(session_id=self._session_id, task_id=task_id, tool=call.tool))

            try:
                plan = {"goal": call.tool, "steps": [dict(call.payload)]}
                success = self._executor.execute(plan)
                result = ToolResult(
                    tool=call.tool,
                    success=success,
                    message="ok" if success else "tool reported failure",
                )
                bus.publish(tool_finished(session_id=self._session_id, task_id=task_id, tool=call.tool, success=success))
                return result
            except TransientError:
                bus.publish(tool_failed(session_id=self._session_id, task_id=task_id, tool=call.tool, error="transient"))
                raise
            except Exception as e:
                bus.publish(tool_failed(session_id=self._session_id, task_id=task_id, tool=call.tool, error=str(e)))
                raise TransientError(f"Tool execution failed: {e}") from e

        return retry(_execute, config=self._retry_config)

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


# --------------------------------------------------
# ADAPTER: existing router/brain/planner -> Orchestrator
# --------------------------------------------------

class LiveOrchestrator:
    """
    Orchestrator that decides what to do with a user utterance.

    It preserves the existing behavior:
    - simple commands go through the command router
    - action requests go through planner + executor
    - everything else goes to the brain
    """

    def __init__(self):
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


# --------------------------------------------------
# WAKE HANDLER HOCK
# --------------------------------------------------

def _make_wake_handler() -> "WakeHandler":
    """Build the backend wake-handoff hook for this runtime."""

    return WakeHandler(
        wake_responses=WAKE_RESPONSES,
        on_wake_start=lambda: bus.publish(wake_listening(session_id=session.id)),
    )


class WakeHandler:
    """Explicit backend hook for wake detection handoff.

    This gives the wake path one clear place to start a session,
    choose a wake response, and announce listening state through
    the bus instead of scattering it across the conversation loop.
    """

    def __init__(
        self,
        *,
        wake_responses: list[str],
        on_wake_start: Callable[[], None],
    ) -> None:
        self.wake_responses = wake_responses
        self.on_wake_start = on_wake_start

    def respond_to_wake(self) -> str:
        self.on_wake_start()
        return random.choice(self.wake_responses)


# --------------------------------------------------
# INITIALIZE
# --------------------------------------------------

print("🚀 Starting JARVIS...")

try:
    validate_backend_startup()
    log_info("backend.startup", "startup validation passed")
except Exception as e:
    log_error("backend.startup", "startup validation failed", {"error": str(e)})
    print(f"❌ Backend startup check failed: {e}")
    os._exit(1)

bus = BackendBus()
bus.subscribe(LoggingObserver(verbose=True))

session = Session("voice-session")
bus.publish(session_started(session))

lifecycle = Lifecycle(bus, session)
lifecycle.mark_started()

print("🧠 Loading Whisper...")

audio = LiveAudioProvider(session_id=session.id)
tool_runner = LiveToolRunner(session_id=session.id)
orchestrator = LiveOrchestrator()

brain = JarvisBrain()
planner = TaskPlanner()

memory = Memory()
router = CommandRouter()


# --------------------------------------------------
# WAKE RESPONSES
# --------------------------------------------------

WAKE_RESPONSES = [
    "Yes?",
    "I'm here.",
    "Go ahead.",
    "Ready.",
    "At your service.",
    "How can I help?",
    "Listening.",
    "What's the mission?",
    "Tell me.",
    "What do you need?",
    "Always ready."
]


# --------------------------------------------------
# SHUTDOWN
# --------------------------------------------------

def is_shutdown_command(text):

    text = re.sub(r"[^a-zA-Z\s]", "", text.lower()).strip()

    phrases = [
        "jarvis shutdown",
        "shutdown jarvis",
        "shut down",
        "goodbye",
        "bye",
        "see you later",
        "exit",
        "quit",
        "im done",
        "i am done",
        "thats all"
    ]

    return any(p in text for p in phrases)


# --------------------------------------------------
# SLEEP
# --------------------------------------------------

def is_sleep_command(text):

    text = text.lower()

    return any(
        p in text for p in [
            "go to sleep",
            "sleep mode",
            "go idle"
        ]
    )


# --------------------------------------------------
# ACTION DETECTOR
# --------------------------------------------------

def is_action_request(text):

    text = text.lower()

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
        "google"
    ]

    return any(word in text for word in action_words)


# --------------------------------------------------
# CONVERSATION
# --------------------------------------------------

def handle_turn(user_text: str) -> bool:
    """Process one user turn through the backend contracts.

    Returns True if the conversation loop should keep running.
    """
    session.note_user(user_text)
    decision = orchestrator.decide(user_text)
    session.note_decision(decision.metadata or {})

    if decision.kind == "reply":
        reply = decision.reply or ""
        print(f"\n🤖 Jarvis: {reply}")
        audio.speak(reply)
        audio.wait()
        session.note_reply(reply)
        return True

    if decision.kind == "action":
        return handle_action(user_text)

    return handle_chat(user_text)


def handle_action(user_text: str) -> bool:
    """Plan and execute one action request through the tool contract."""
    print("\n🤖 Jarvis: Working on it.")
    audio.speak("Working on it.")
    audio.wait()

    try:
        task = orchestrator.plan_action(user_text)
    except Exception as e:
        print(f"❌ Planning failed: {e}")
        audio.speak("I couldn't plan that task. Please check that Ollama is running.")
        audio.wait()
        return True

    session.active_task = task
    bus.publish(task_started(task, session_id=session.id))

    task.start()

    try:
        for step in task.steps:
            if lifecycle.shutdown_requested:
                session.cancel_active_task()
                return False

            call = ToolCall(tool=step["tool"], payload={k: v for k, v in step.items() if k != "tool"})
            result = tool_runner.run(call, task=task)

            if not result.success:
                task.fail(result.message or "tool failed")
                bus.publish(task_failed(task, session_id=session.id))
                print(f"\n🤖 Jarvis: I couldn't complete that task.")
                audio.speak("I couldn't complete that task.")
                audio.wait()
                return True

        task.complete("done")
        bus.publish(task_completed(task, session_id=session.id))

        print("\n🤖 Jarvis: Done.")
        audio.speak("Done.")
        audio.wait()

        return True

    except TransientError as e:
        task.fail(str(e))
        bus.publish(task_failed(task, session_id=session.id))
        print(f"\n🤖 Jarvis: I couldn't complete that task.")
        audio.speak("I couldn't complete that task.")
        audio.wait()
        return True
    except Exception as e:
        task.fail(f"unexpected error: {e}")
        bus.publish(task_failed(task, session_id=session.id))
        print(f"\n🤖 Jarvis: Something went wrong.")
        audio.speak("Something went wrong.")
        audio.wait()
        return True
    finally:
        session.active_task = None


def handle_chat(user_text: str) -> bool:
    """Reply to a general knowledge request through the brain."""
    memories = memory.search_memories(user_text)

    if memories:
        context = f"Relevant memories:\n{memories}\n\nUser: {user_text}"
    else:
        context = user_text

    print("🤖 Jarvis:", end=" ", flush=True)

    full_reply = ""
    first = True
    start = time.time()

    for sentence in brain.stream(context):

        if first:
            print(f"\n⚡ First response: {time.time()-start:.2f}s")
            print("🤖 Jarvis:", end=" ", flush=True)
            first = False

        print(sentence, end=" ", flush=True)
        audio.speak(sentence)
        full_reply += sentence + " "

    audio.wait()
    print()

    memory.save_memory(user_text, full_reply.strip())
    session.note_reply(full_reply.strip())

    return True


def conversation():
    """Main wake-driven conversation loop using the backend contracts."""

    wake = random.choice(WAKE_RESPONSES)

    print(f"\n🤖 Jarvis: {wake}")
    audio.speak(wake)
    audio.wait()

    if lifecycle.shutdown_requested:
        return

    wake_handler = _make_wake_handler()

    bus.publish(wake_detected(session_id=session.id))

    wake_response = wake_handler.respond_to_wake()
    bus.publish(wake_listening(session_id=session.id))
    audio.speak(wake_response)
    audio.wait()

    if lifecycle.shutdown_requested:
        return

    timeout = time.time() + 30

    while time.time() < timeout:

        print("\n🎤 Listening...")

        audio_path = audio.record_audio()
        user = audio.transcribe(audio_path).strip()

        if not user:
            continue

        print(f"🧑 You: {user}")
        bus.publish(transcription_ready(session_id=session.id, user_text=user))

        # ---------------- Shutdown ----------------

        if is_shutdown_command(user):
            reply = "Shutting down. Goodbye, Shafi."

            print(f"\n🤖 Jarvis: {reply}")
            audio.speak(reply)
            audio.wait()

            lifecycle.request_shutdown()
            lifecycle.shutdown()
            os._exit(0)

        # ---------------- Sleep ----------------

        if is_sleep_command(user):
            reply = "Going back to sleep."

            print(f"\n🤖 Jarvis: {reply}")
            audio.speak(reply)
            audio.wait()

            lifecycle.request_shutdown()
            return

        # ---------------- User interrupt ----------------

        if lifecycle.shutdown_requested:
            bus.publish(user_interrupt(session_id=session.id))
            return False

        if not handle_turn(user):
            return False

        timeout = time.time() + 30


# --------------------------------------------------
# START
# --------------------------------------------------

if __name__ == "__main__":
    WakeWordDetector(conversation).start()
