"""
Voice → Brain → Response loop

Loads WhisperModel once, then loops:
  record 5s → transcribe → send to Dell /api/v1/chat → print response → repeat

Press Ctrl+C to exit.

Flow:
  🎤 Microphone → sounddevice → Faster-Whisper → text → Dell /api/v1/chat → Lenovo brain → response
"""

import sys
import time
import json
import urllib.request
import urllib.error
import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DURATION = 5          # seconds to record
SAMPLE_RATE = 16000   # 16 kHz mono
CHAT_URL = "http://127.0.0.1:8000/api/v1/chat"


def record_audio():
    """Record from the default microphone for DURATION seconds."""
    import sounddevice as sd

    print(f"🎤 Recording for {DURATION} seconds — speak now...")
    t0 = time.perf_counter()
    audio = sd.rec(
        int(DURATION * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
    )
    sd.wait()
    elapsed = time.perf_counter() - t0
    print(f"🎤 Recording complete. ({elapsed:.2f}s)\n")
    return audio.flatten()


def transcribe(model, audio):
    """Transcribe a numpy float32 array with a pre-loaded WhisperModel."""
    print("⏳ Transcribing...")
    t0 = time.perf_counter()
    segments, info = model.transcribe(audio, language="en")
    text_parts = [seg.text.strip() for seg in segments if seg.text.strip()]
    transcript = " ".join(text_parts) if text_parts else ""
    transcribe_time = time.perf_counter() - t0
    print(f"⏳ Transcription complete. ({transcribe_time:.2f}s)")

    print(f'🗣️  You said: "{transcript}"')
    print(f"   (detected language: {info.language}, probability: {info.language_probability:.2f})\n")
    return transcript


def chat(transcript):
    """Send the transcript to the Dell /api/v1/chat endpoint and return the response."""
    payload = json.dumps({
        "messages": [{"role": "user", "content": transcript}],
    }).encode("utf-8")

    req = urllib.request.Request(
        CHAT_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("response", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"❌ HTTP {exc.code}: {detail}")
        return ""
    except urllib.error.URLError as exc:
        print(f"❌ Connection error: {exc.reason}")
        return ""


def main():
    # ------------------------------------------------------------------
    # Load Whisper model once
    # ------------------------------------------------------------------
    from faster_whisper import WhisperModel

    print("⏳ Loading Whisper model (one-time)...")
    t0 = time.perf_counter()
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    model_time = time.perf_counter() - t0
    print(f"⏳ WhisperModel ready. ({model_time:.2f}s)\n")
    print("Voice loop active — press Ctrl+C to exit.\n")

    # ------------------------------------------------------------------
    # Voice loop
    # ------------------------------------------------------------------
    try:
        while True:
            t_start = time.perf_counter()

            # 1. Record
            audio = record_audio()
            if np.max(np.abs(audio)) < 0.01:
                print("⚠️  Audio is very quiet — you may not have spoken. Try again closer to the mic.\n")
                continue

            # 2. Transcribe
            transcript = transcribe(model, audio)
            if not transcript:
                print("⏭️  No speech detected — skipping.\n")
                continue

            # 3. Chat
            print("🧠 Sending to JARVIS...")
            t0 = time.perf_counter()
            response = chat(transcript)
            chat_time = time.perf_counter() - t0
            print(f"   (chat request: {chat_time:.2f}s)")

            if response:
                print(f"\n🤖 JARVIS: {response}\n")
            else:
                print("❌ No response from JARVIS.\n")
                continue

            total = time.perf_counter() - t_start
            print(f"⏱️  Total turn: {total:.2f}s\n")

    except KeyboardInterrupt:
        print("\n👋 Exiting voice loop.")


if __name__ == "__main__":
    main()
