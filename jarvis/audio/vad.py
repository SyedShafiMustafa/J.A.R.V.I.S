import queue
import sounddevice as sd
import soundfile as sf
import numpy as np
import tempfile
import time


class VoiceRecorder:

    def __init__(self):
        self.sample_rate = 16000
        self.channels = 1

    def record(self):

        print("🎤 Speak...")

        q = queue.Queue()

        def callback(indata, frames, t, status):
            q.put(indata.copy())

        recording = []

        silence = 0
        started = False

        with sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            callback=callback,
        ):

            while True:

                data = q.get()

                audio = data.flatten()

                volume = np.abs(audio).mean()

                if volume > 0.015:
                    started = True
                    silence = 0

                if started:
                    recording.append(audio)

                if started and volume < 0.008:
                    silence += 1
                else:
                    silence = 0

                # ~0.5 second silence
                if started and silence > 15:
                    break

        audio = np.concatenate(recording)

        path = tempfile.NamedTemporaryFile(
            suffix=".wav",
            delete=False
        ).name

        sf.write(path, audio, self.sample_rate)

        return path