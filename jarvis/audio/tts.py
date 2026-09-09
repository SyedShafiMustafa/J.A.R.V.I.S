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

        self.worker = threading.Thread(target=self._speaker_loop, daemon=True)
        self.worker.start()

    def _speaker_loop(self):
        while self.running:
            text = self.queue.get()

            if text is None:
                break

            if self._stop_event.is_set():
                self._stop_event.clear()
                self.queue.task_done()
                continue

            chunks = []

            for chunk in self.voice.synthesize(text):
                chunks.append(chunk.audio_int16_array)

            if chunks:
                audio = np.concatenate(chunks).astype(np.float32) / 32768.0

                silence = np.zeros(int(self.sample_rate * 0.20), dtype=np.float32)
                audio = np.concatenate([audio, silence])

                self._stop_event.clear()
                self._current_stream = sd.OutputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                )
                self._current_stream.start()
                self._current_stream.write(audio)

                # Wait for playback to complete, but check for stop event
                chunk_duration = len(audio) / self.sample_rate
                elapsed = 0.0
                poll_interval = 0.02  # 20ms
                while elapsed < chunk_duration and not self._stop_event.is_set():
                    time.sleep(poll_interval)
                    elapsed += poll_interval

                if self._current_stream:
                    self._current_stream.stop()
                    self._current_stream.close()
                    self._current_stream = None

                if self._stop_event.is_set():
                    self._stop_event.clear()
                    self.queue.task_done()
                    continue

            self.queue.task_done()

    def speak(self, text: str):
        if text.strip():
            self.queue.put(text)

    def wait(self):
        self.queue.join()

    def stop(self):
        sd.stop()
        self.running = False
        self.queue.put(None)

    def stop_speaking(self):
        """Immediately stop current playback. Thread-safe."""
        self._stop_event.set()
        if self._current_stream:
            try:
                self._current_stream.stop()
                self._current_stream.close()
            except Exception:
                pass
            self._current_stream = None
        # Clear any pending text in queue
        try:
            while True:
                self.queue.get_nowait()
                self.queue.task_done()
        except queue.Empty:
            pass
        sd.stop()