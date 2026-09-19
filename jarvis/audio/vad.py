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
                 pre_buffer_seconds=1.0, speech_threshold=0.015, silence_threshold=0.008,
                 silence_frames=16):
        self.sample_rate = 16000
        self.channels = 1
        self.max_duration = max_duration
        self.speech_wait_timeout = speech_wait_timeout
        self.speech_start_frames = speech_start_frames
        # Absolute floors. A block counts as speech above speech_threshold;
        # end-of-speech fires after silence_frames quiet blocks. The effective
        # end-of-speech level adapts upward in a noisy room (see record()).
        self.speech_threshold = speech_threshold
        self.silence_threshold = silence_threshold
        self.silence_frames = silence_frames
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
        # Slow estimate of the room's ambient level, measured before speech.
        ambient = 0.0

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
                # No-speech guard is checked every iteration, not only when the
                # block queue is empty: a live microphone keeps delivering
                # blocks (ambient noise), so queue.Empty never fires and the
                # wait would otherwise run until max_duration.
                if not started and elapsed >= self.speech_wait_timeout:
                    raise NoSpeechError("no speech detected before timeout")

                try:
                    data = q.get(timeout=0.1)
                except queue.Empty:
                    if not started and elapsed >= self.speech_wait_timeout:
                        raise NoSpeechError("no speech detected before timeout")
                    continue

                audio = data.flatten()

                volume = np.abs(audio).mean()

                # Ambient (room-noise) estimate, tracked only before speech is
                # detected and only on non-speech-loud blocks, so the utterance
                # itself cannot inflate it.
                if not started and volume <= self.speech_threshold:
                    ambient += (volume - ambient) * 0.05

                # End-of-speech level: the fixed floor, raised above a noisy
                # room's ambient level so fan/HVAC/keyboard noise cannot keep
                # the recording alive until max_duration.
                silence_level = max(self.silence_threshold, ambient * 1.5)

                # Speech-start accumulator: sustained speech fills one block
                # at a time (same onset confirmation as before), but brief
                # dips (word gaps) decay instead of erasing progress. Natural
                # speech rarely holds 12 *consecutive* loud blocks — with a
                # hard reset the trigger often never fires at all and the
                # utterance is lost entirely.
                if volume > self.speech_threshold:
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

                if started:
                    recording.append(audio)

                # End-of-speech counter: quiet blocks advance it, louder
                # blocks decay it. Decaying (rather than hard-resetting) lets
                # isolated room-noise spikes pass without erasing progress,
                # while still requiring ~0.4 s of sustained quiet to stop.
                if started:
                    if volume < silence_level:
                        silence += 1
                    else:
                        silence = max(silence - 1, 0)

                if started and silence >= self.silence_frames:
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