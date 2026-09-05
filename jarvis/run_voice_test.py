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


# --------------------------------------------------
# INITIALIZE
# --------------------------------------------------

print("🚀 Starting JARVIS...")
print("🧠 Loading Whisper...")

recorder = VoiceRecorder()
stt = SpeechToText()
tts = TextToSpeech()

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