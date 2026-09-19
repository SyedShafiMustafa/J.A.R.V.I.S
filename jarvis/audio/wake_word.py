import threading

import sounddevice as sd
import numpy as np
from openwakeword.model import Model

from config.config import SAMPLE_RATE, WAKEWORD


class WakeWordDetector:
    def __init__(self, on_detect, on_error=None, on_started=None):
        self.on_detect = on_detect
        self.on_error = on_error
        self.on_started = on_started
        self.model = Model(inference_framework="onnx")
        self.triggered = False
        self.busy = False
        self._stop_event = None
        self._stream = None
        self._lock = threading.Lock()
        self._error_reported = False

    def _report_error(self, error: Exception) -> None:
        with self._lock:
            if self._error_reported:
                return
            self._error_reported = True
        if self.on_error is not None:
            self.on_error(error)

    def stop(self):
        with self._lock:
            stop_event = self._stop_event
            stream = self._stream
        if stop_event is not None:
            stop_event.set()
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()

    def _handle_detection(self):
        try:
            self.on_detect()
        except Exception as e:
            self._report_error(e)
        finally:
            self.busy = False

    def start(self, stop_event=None):
        print("[WAKE] Listening for 'Hey Jarvis'...")
        stop_event = stop_event or threading.Event()
        with self._lock:
            self._stop_event = stop_event
            self._error_reported = False

        def callback(indata, frames, time, status):
            try:
                audio = (indata[:, 0] * 32767).astype(np.int16)
                prediction = self.model.predict(audio)

                score = prediction.get(WAKEWORD, 0.0)

                if score > 0.5 and not self.triggered and not self.busy:
                    self.triggered = True
                    self.busy = True
                    print("[WAKE] Wake word detected!")

                    # Run the conversation on its own thread — never block
                    # the audio callback (that causes buffer overflows).
                    threading.Thread(target=self._handle_detection, daemon=True).start()

                if score < 0.2:
                    self.triggered = False
            except Exception as exc:
                self._report_error(exc)
                stop_event.set()

        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=1280,
                callback=callback,
            )
        except Exception as exc:
            self._report_error(exc)
            raise
        with self._lock:
            self._stream = stream
        try:
            stream.start()
            if self.on_started is not None:
                self.on_started()
            # Block on the stop event (no 100 ms poll), so the microphone
            # stream is released immediately on stop and the wake->command
            # handoff does not pay an extra polling delay.
            stop_event.wait()
        except Exception as exc:
            self._report_error(exc)
            raise
        finally:
            try:
                stream.stop()
            finally:
                stream.close()
            with self._lock:
                if self._stream is stream:
                    self._stream = None
                if self._stop_event is stop_event:
                    self._stop_event = None