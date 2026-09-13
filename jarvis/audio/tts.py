from piper import PiperVoice
import sounddevice as sd
import numpy as np
import threading
import queue
import time

from config.config import PIPER_MODEL


class TextToSpeech:
    def __init__(self):
        print("[TTS] Loading Piper voice...")

        self.voice = PiperVoice.load(str(PIPER_MODEL))
        self.sample_rate = 22050

        self.queue = queue.Queue()
        self.running = True
        self._current_stream = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self.worker = threading.Thread(target=self._speaker_loop, daemon=True)
        self.worker.start()

    def _speaker_loop(self):
        while self.running:
            text = self.queue.get()
            try:
                if text is None:
                    break
                if self._stop_event.is_set():
                    continue

                chunks = []
                for chunk in self.voice.synthesize(text):
                    if self._stop_event.is_set():
                        break
                    chunks.append(chunk.audio_int16_array)

                if chunks and not self._stop_event.is_set():
                    audio = np.concatenate(chunks).astype(np.float32) / 32768.0
                    silence = np.zeros(int(self.sample_rate * 0.20), dtype=np.float32)
                    audio = np.concatenate([audio, silence])
                    stream = sd.OutputStream(
                        samplerate=self.sample_rate,
                        channels=1,
                        dtype="float32",
                    )
                    with self._lock:
                        self._current_stream = stream
                    try:
                        stream.start()
                        stream.write(audio)
                        chunk_duration = len(audio) / self.sample_rate
                        elapsed = 0.0
                        while elapsed < chunk_duration and not self._stop_event.is_set():
                            time.sleep(0.02)
                            elapsed += 0.02
                    finally:
                        try:
                            stream.stop()
                        finally:
                            stream.close()
                        with self._lock:
                            if self._current_stream is stream:
                                self._current_stream = None
            finally:
                self.queue.task_done()

    def speak(self, text: str):
        if text.strip():
            self._stop_event.clear()
            self.queue.put(text)

    def wait(self):
        self.queue.join()

    def stop(self):
        sd.stop()
        with self._lock:
            self.running = False
            stream = self._current_stream
        self._stop_event.set()
        if stream is not None:
            stream.stop()
            stream.close()
        self.queue.put(None)
        self.worker.join(timeout=2.0)
        if self.worker.is_alive():
            raise RuntimeError("TTS worker did not stop within 2 seconds")

    def stop_speaking(self):
        """Immediately stop current playback. Thread-safe."""
        self._stop_event.set()
        with self._lock:
            stream = self._current_stream
        if stream:
            try:
                stream.stop()
            finally:
                stream.close()
            with self._lock:
                if self._current_stream is stream:
                    self._current_stream = None
        # Clear any pending text in queue
        try:
            while True:
                self.queue.get_nowait()
                self.queue.task_done()
        except queue.Empty:
            pass
        sd.stop()