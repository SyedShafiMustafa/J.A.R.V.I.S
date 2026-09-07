"""
Voice → Brain → Response loop

Loads WhisperModel once, then loops:
  wait for speech → record while speaking → stop after silence → transcribe → chat → speak → repeat

Uses energy-based voice activity detection (VAD) and pyttsx3 for TTS.

Press Ctrl+C to exit.

Flow:
  🎤 Microphone → VAD → Faster-Whisper → text → Dell /api/v1/chat → Lenovo brain → response → 🔊 speaker
"""

import os
import sys
import time
import json
import urllib.request
import urllib.error
import numpy as np

# Fix Windows console encoding for emoji output
if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000   # 16 kHz mono
CHAT_URL = "http://127.0.0.1:8001/v1/chat/stream"

# VAD settings (energy-based)
SILENCE_THRESHOLD = 0.01   # RMS below this = silence
PRE_SPEECH_buffer = 0.3    # seconds of audio to keep before speech onset
POST_SPEECH_TIMEOUT = 1.5  # seconds of silence after speech ends → stop
MIN_SPEECH_DURATION = 0.3  # ignore very short blinks
MAX_RECORD_SECONDS = 30    # hard cap to avoid runaway recording
CHUNK_SECONDS = 0.1        # read audio in 100ms chunks


def _rms(audio_chunk):
    """Root-mean-square of a numpy float32 array."""
    return float(np.sqrt(np.mean(audio_chunk.astype(np.float64) ** 2)))


def record_audio():
    """Record audio using voice activity detection.

    - Waits silently until speech begins.
    - Records while speech is present.
    - Stops POST_SPEECH_TIMEOUT seconds after speech ends.
    - Returns the captured audio as a flat float32 numpy array.
    """
    import sounddevice as sd

    chunk_size = int(SAMPLE_RATE * CHUNK_SECONDS)
    pre_speech_chunks = max(1, int(PRE_SPEECH_buffer / CHUNK_SECONDS))
    post_speech_chunks = int(POST_SPEECH_TIMEOUT / CHUNK_SECONDS)
    max_chunks = int(MAX_RECORD_SECONDS / CHUNK_SECONDS)

    state = "waiting"     # waiting → recording → tail → done
    chunks = []
    pre_speech_buf = []
    post_silence_count = 0

    print("🎤 Listening — speak when ready...")

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=chunk_size,
    ) as stream:
        for _ in range(max_chunks):
            data, overflowed = stream.read(chunk_size)
            chunk = data.flatten()
            rms = _rms(chunk)

            if state == "waiting":
                # Keep a rolling buffer of pre-speech audio
                pre_speech_buf.append(chunk)
                if len(pre_speech_buf) > pre_speech_chunks:
                    pre_speech_buf.pop(0)
                if rms >= SILENCE_THRESHOLD:
                    # Speech detected — include pre-speech buffer for context
                    chunks.extend(pre_speech_buf)
                    chunks.append(chunk)
                    state = "recording"
                    post_silence_count = 0

            elif state == "recording":
                chunks.append(chunk)
                if rms < SILENCE_THRESHOLD:
                    post_silence_count += 1
                else:
                    post_silence_count = 0

                # Stop after enough consecutive silence
                if post_silence_count >= post_speech_chunks:
                    state = "done"
                    break

            elif state == "done":
                break

    if not chunks:
        print("🎤 No audio captured.\n")
        return np.array([], dtype=np.float32)

    audio = np.concatenate(chunks)
    duration = len(audio) / SAMPLE_RATE
    print(f"🎤 Recording complete. ({duration:.2f}s)\n")
    return audio


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


def speak(text):
    """Speak text aloud using a fresh pyttsx3 engine.

    A new engine is created per call because the SAPI5 COM driver
    silently disconnects after the first runAndWait() on Windows.
    """
    import pyttsx3

    if not text:
        return
    print("🔊 Speaking...")
    t0 = time.perf_counter()
    engine = pyttsx3.init()
    engine.setProperty("rate", 175)
    engine.setProperty("volume", 1.0)
    engine.say(text)
    engine.runAndWait()
    engine.stop()
    elapsed = time.perf_counter() - t0
    print(f"🔊 Done speaking. ({elapsed:.2f}s)")


SENTENCE_ENDINGS = (".", "?", "!")


def chat_stream(transcript):
    """Send the transcript to the Lenovo brain streaming endpoint.

    Reads server-sent events and yields complete sentences as soon as they
    arrive, so the caller can start speaking the first sentence while the
    brain is still generating the rest.
    """
    payload = json.dumps({
        "messages": [{"role": "user", "content": transcript}],
    }).encode("utf-8")

    req = urllib.request.Request(
        CHAT_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    pending = ""

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data:
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")
                if etype == "error":
                    print(f"❌ Brain stream error: {event.get('detail', 'unknown')}")
                    continue
                if etype == "done":
                    break
                if etype == "sentence":
                    pending += event.get("sentence", "")
                    # Only hand over complete sentences — never interrupt one halfway.
                    if pending.endswith(SENTENCE_ENDINGS):
                        complete = pending.strip()
                        pending = ""
                        if complete:
                            yield complete

        # Flush any remaining text once the stream has finished.
        if pending.strip():
            yield pending.strip()

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"❌ HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        print(f"❌ Connection error: {exc.reason}")


def main():
    # ------------------------------------------------------------------
    # Load Whisper model once
    # ------------------------------------------------------------------
    from faster_whisper import WhisperModel

    print("⏳ Loading Whisper model (one-time)...")
    t0 = time.perf_counter()
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    model_time = time.perf_counter() - t0
    print(f"⏳ WhisperModel ready. ({model_time:.2f}s)")

    print("Voice loop active — press Ctrl+C to exit.\n")

    # ------------------------------------------------------------------
    # Voice loop
    # ------------------------------------------------------------------
    try:
        while True:
            t_start = time.perf_counter()

            # 1. Record (VAD-based)
            audio = record_audio()
            if len(audio) == 0:
                continue

            duration = len(audio) / SAMPLE_RATE
            if duration < MIN_SPEECH_DURATION:
                print("⏭️  Recording too short — skipping.\n")
                continue

            # 2. Transcribe
            transcript = transcribe(model, audio)
            if not transcript:
                print("⏭️  No speech detected — skipping.\n")
                continue

            # 3. Chat (streaming) — speak each sentence as it arrives
            print("🧠 Sending to JARVIS (streaming)...")
            t0 = time.perf_counter()
            first_sentence_time = None
            sentence_count = 0
            for sentence in chat_stream(transcript):
                sentence_count += 1
                if first_sentence_time is None:
                    first_sentence_time = time.perf_counter() - t0
                    print(f"   (first sentence in {first_sentence_time:.2f}s)")
                print(f"\n🤖 JARVIS: {sentence}\n")
                # 4. Speak immediately — later sentences may still be arriving
                speak(sentence)
            chat_time = time.perf_counter() - t0
            print(f"   (streaming chat + speech: {chat_time:.2f}s)")

            if sentence_count == 0:
                print("❌ No response from JARVIS.\n")
                continue

            total = time.perf_counter() - t_start
            print(f"⏱️  Total turn: {total:.2f}s\n")

    except KeyboardInterrupt:
        print("\n👋 Exiting voice loop.")


if __name__ == "__main__":
    main()
