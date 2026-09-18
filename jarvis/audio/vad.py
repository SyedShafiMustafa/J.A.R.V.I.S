import queue
from collections import deque

import sounddevice as sd
import soundfile as sf
import numpy as np
import tempfile
import time
import threading


class RecordingOutcomeError(Exception):
    """Base class for expected recording outcomes."""


class NoSpeechError(RecordingOutcomeError):
    pass


class RecordingTimeoutError(RecordingOutcomeError):
    pass


class RecordingCancelledError(RecordingOutcomeError):
    pass


class VoiceRecorder:

    def __init__(self, max_duration=30.0, speech_wait_timeout=5.0, speech_start_frames=12,
                 pre_buffer_seconds=1.0):
        self.sample_rate = 16000
        self.channels = 1
        self.max_duration = max_duration
        self.speech_wait_timeout = speech_wait_timeout
        self.speech_start_frames = speech_start_frames
        # Audio captured just before speech is confirmed is kept here so the
        # first word is not clipped while the speech_start_frames threshold
        # accumulates.
        self.pre_buffer_seconds = pre_buffer_seconds

    def record(self, cancel_event=None):

        print("[VAD] Speak...")

        q = queue.Queue()

        def callback(indata, frames, t, status):
            q.put(indata.copy())

        recording = []

        # Rolling window of audio preceding the speech-start trigger; flushed
        # into the recording the moment speech begins.
        max_pre_samples = int(self.pre_buffer_seconds * self.sample_rate)
        pre_buffer = deque()
        pre_buffer_samples = 0

        silence = 0
        started = False
        speech_frames = 0

        cancel_event = cancel_event or threading.Event()
        started_at = time.monotonic()
        stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            callback=callback,
        )
        try:
            stream.start()
            while True:
                elapsed = time.monotonic() - started_at
                if cancel_event.is_set():
                    raise RecordingCancelledError("recording cancelled")
                if elapsed >= self.max_duration:
                    raise RecordingTimeoutError("maximum recording duration reached")

                try:
                    data = q.get(timeout=0.1)
                except queue.Empty:
                    if not started and elapsed >= self.speech_wait_timeout:
                        raise NoSpeechError("no speech detected before timeout")
                    continue

                audio = data.flatten()

                volume = np.abs(audio).mean()

                # Speech-start accumulator: sustained speech fills one block
                # at a time (same onset confirmation as before), but brief
                # dips (word gaps) decay instead of erasing progress. Natural
                # speech rarely holds 12 *consecutive* loud blocks — with a
                # hard reset the trigger often never fires at all and the
                # utterance is lost entirely.
                if volume > 0.015:
                    speech_frames += 1
                else:
                    speech_frames = max(speech_frames - 1, 0)

                if not started and speech_frames >= self.speech_start_frames:
                    started = True
                    recording.extend(pre_buffer)
                    pre_buffer.clear()

                if not started:
                    pre_buffer.append(audio)
                    pre_buffer_samples += len(audio)
                    while pre_buffer_samples > max_pre_samples and pre_buffer:
                        pre_buffer_samples -= len(pre_buffer.popleft())

                if started and volume > 0.015:
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
        finally:
            try:
                stream.stop()
            finally:
                stream.close()

        if not recording:
            raise NoSpeechError("no speech detected")
        audio = np.concatenate(recording)

        path = tempfile.NamedTemporaryFile(
            suffix=".wav",
            delete=False
        ).name

        sf.write(path, audio, self.sample_rate)

        return path