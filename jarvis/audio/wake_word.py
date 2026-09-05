import threading

import sounddevice as sd
import numpy as np
from openwakeword.model import Model

from config.config import SAMPLE_RATE, WAKEWORD


class WakeWordDetector:
    def __init__(self, on_detect):
        self.on_detect = on_detect
        self.model = Model(inference_framework="onnx")
        self.triggered = False
        self.busy = False

    def _handle_detection(self):
        try:
            self.on_detect()
        except Exception as e:
            print(f"❌ Conversation error: {e}")
        finally:
            self.busy = False

    def start(self):
        print("🎙 Listening for 'Hey Jarvis'...")

        def callback(indata, frames, time, status):
            audio = (indata[:, 0] * 32767).astype(np.int16)
            prediction = self.model.predict(audio)

            score = prediction.get(WAKEWORD, 0.0)

            if score > 0.5 and not self.triggered and not self.busy:
                self.triggered = True
                self.busy = True
                print("✅ Wake word detected!")

                # Run the conversation on its own thread — never block
                # the audio callback (that causes buffer overflows).
                threading.Thread(target=self._handle_detection, daemon=True).start()

            if score < 0.2:
                self.triggered = False

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=1280,
            callback=callback,
        ):
            while True:
                sd.sleep(1000)