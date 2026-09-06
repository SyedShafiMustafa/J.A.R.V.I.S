import os
import re
import sys
import time
import random
from pathlib import Path

# Make imports work no matter which directory this script is run from
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.validation import validate_backend_startup
from backend.observability import log_info, log_error
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
    TransientError,
)
from backend.live_adapters import LiveAudioProvider, LiveToolRunner, LiveOrchestrator


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

audio = LiveAudioProvider(bus=bus, session_id=session.id)
tool_runner = LiveToolRunner(bus=bus, session_id=session.id)
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
