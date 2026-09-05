from piper import PiperVoice
import sounddevice as sd
import numpy as np
import threading
import queue

from config.config import PIPER_MODEL


class TextToSpeech:
    def __init__(self):
        print("🔊 Loading Piper voice...")

        self.voice = PiperVoice.load(str(PIPER_MODEL))
        self.sample_rate = 22050

        self.queue = queue.Queue()
        self.running = True

        self.worker = threading.Thread(target=self._speaker_loop, daemon=True)
        self.worker.start()

    def _speaker_loop(self):
        while self.running:
            text = self.queue.get()

            if text is None:
                break

            chunks = []

            for chunk in self.voice.synthesize(text):
                chunks.append(chunk.audio_int16_array)

            if chunks:
                audio = np.concatenate(chunks).astype(np.float32) / 32768.0

                silence = np.zeros(int(self.sample_rate * 0.20), dtype=np.float32)
                audio = np.concatenate([audio, silence])

                sd.play(audio, self.sample_rate)
                sd.wait()

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