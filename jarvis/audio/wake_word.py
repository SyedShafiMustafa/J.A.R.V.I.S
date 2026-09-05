import sounddevice as sd
import numpy as np
from openwakeword.model import Model
from config.config import SAMPLE_RATE


class WakeWordDetector:
    def __init__(self, on_detect):
        self.on_detect = on_detect
        self.model = Model(inference_framework="onnx")
        self.triggered = False

    def start(self):
        print("🎙 Listening for 'Hey Jarvis'...")

        def callback(indata, frames, time, status):
            audio = (indata[:, 0] * 32767).astype(np.int16)
            prediction = self.model.predict(audio)

            score = prediction.get("hey_jarvis", 0.0)

            if score > 0.5 and not self.triggered:
                self.triggered = True
                print("✅ Wake word detected!")
                self.on_detect()

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