import os
import re
import sys
import time
import random
from pathlib import Path

# Make imports work no matter which directory this script is run from
sys.path.insert(0, str(Path(__file__).resolve().parent))

from audio.wake_word import WakeWordDetector
from audio.vad import VoiceRecorder
from audio.stt import SpeechToText
from audio.tts import TextToSpeech

from agents.brain import JarvisBrain
from agents.planner import TaskPlanner

from tools.executor import TaskExecutor

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
    PermanentError,
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

    def __init__(self):
        self.recorder = VoiceRecorder()
        self.stt = SpeechToText()
        self.tts = TextToSpeech()
        self._wake_detector: WakeWordDetector | None = None

    def start_wake_word(self) -> None:
        if self._wake_detector is not None:
            raise RuntimeError("Wake word detector already running")

        self._wake_detector = WakeWordDetector(on_detect=self._on_wake_detected)
        self._wake_detector.start()

    def stop_wake_word(self) -> None:
        if self._wake_detector is None:
            return

        self._wake_detector = None

    def record_audio(self) -> str:
        return self.recorder.record()

    def transcribe(self, audio_path: str) -> str:
        return self.stt.transcribe(audio_path)

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

    def __init__(self):
        self._executor = TaskExecutor()

    def run(self, call: ToolCall) -> ToolResult:
        try:
            plan = {"goal": call.tool, "steps": [dict(call.payload)]}
            success = self._executor.execute(plan)
            return ToolResult(
                tool=call.tool,
                success=success,
                message="ok" if success else "tool reported failure",
            )
        except Exception as e:
            raise TransientError(f"Tool execution failed: {e}") from e


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
# INITIALIZE
# --------------------------------------------------

print("🚀 Starting JARVIS...")
print("🧠 Loading Whisper...")

audio = LiveAudioProvider()
tool_runner = LiveToolRunner()
orchestrator = LiveOrchestrator()

brain = JarvisBrain()
planner = TaskPlanner()
executor = TaskExecutor()

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

def conversation():

    wake = random.choice(WAKE_RESPONSES)

    print(f"\n🤖 Jarvis: {wake}")
    tts.speak(wake)
    tts.wait()

    timeout = time.time() + 30

    while time.time() < timeout:

        print("\n🎤 Listening...")

        audio = recorder.record()
        user = stt.transcribe(audio).strip()

        if not user:
            continue

        print(f"🧑 You: {user}")

        # ---------------- Shutdown ----------------

        if is_shutdown_command(user):

            reply = "Shutting down. Goodbye, Shafi."

            print(f"🤖 Jarvis: {reply}")

            tts.speak(reply)
            tts.wait()

            os._exit(0)

        # ---------------- Sleep ----------------

        if is_sleep_command(user):

            reply = "Going back to sleep."

            print(f"🤖 Jarvis: {reply}")

            tts.speak(reply)
            tts.wait()

            return

        # ---------------- Router ----------------

        handled, reply = router.route(user)

        if handled:

            print(f"🤖 Jarvis: {reply}")

            tts.speak(reply)
            tts.wait()

            timeout = time.time() + 30
            continue

        # ==================================================
        # ACTIONS → Planner + Executor
        # ==================================================

        if is_action_request(user):

            # ---------- Plan ----------

            try:
                plan = planner.create_plan(user)
            except Exception as e:
                print(f"❌ Planning failed: {e}")
                tts.speak("I couldn't plan that task. Please check that Ollama is running.")
                tts.wait()
                timeout = time.time() + 30
                continue

            # ---------- Execute ----------

            print("🤖 Jarvis: Working on it.")
            tts.speak("Working on it.")
            tts.wait()

            success = executor.execute(plan)

            if success:
                print("🤖 Jarvis: Done.")
                tts.speak("Done.")
            else:
                print("🤖 Jarvis: I couldn't complete that task.")
                tts.speak("I couldn't complete that task.")

            tts.wait()

            timeout = time.time() + 30
            continue

        # ==================================================
        # KNOWLEDGE → Brain
        # ==================================================

        memories = memory.search_memories(user)

        context = user

        if memories:
            context = f"Relevant memories:\n{memories}\n\nUser: {user}"

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

            tts.speak(sentence)

            full_reply += sentence + " "

        tts.wait()
        print()

        memory.save_memory(user, full_reply.strip())

        timeout = time.time() + 30


# --------------------------------------------------
# START
# --------------------------------------------------

if __name__ == "__main__":
    WakeWordDetector(conversation).start()
