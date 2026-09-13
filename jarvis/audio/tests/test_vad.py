import threading
import time

import numpy as np
import pytest

from audio import vad


class FakeStream:
    def __init__(self, batches):
        self.batches = batches
        self.callback = None
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True
        for batch in self.batches:
            self.callback(batch, len(batch), None, None)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def install_stream(monkeypatch, batches):
    stream = FakeStream(batches)

    def make_stream(**kwargs):
        stream.callback = kwargs["callback"]
        return stream

    monkeypatch.setattr(vad.sd, "InputStream", make_stream)
    monkeypatch.setattr(vad.sf, "write", lambda *args, **kwargs: None)
    monkeypatch.setattr(vad.tempfile, "NamedTemporaryFile", lambda **kwargs: type(
        "Temp", (), {"name": "capture.wav"}
    )())
    return stream


def audio_batch(volume):
    return np.full((32, 1), volume, dtype=np.float32)


def test_speech_and_silence_complete_recording(monkeypatch):
    stream = install_stream(
        monkeypatch,
        [audio_batch(0.02)] + [audio_batch(0.0)] * 16,
    )

    path = vad.VoiceRecorder(
        max_duration=1,
        speech_wait_timeout=1,
    ).record()

    assert path == "capture.wav"
    assert stream.started and stream.stopped and stream.closed


@pytest.mark.parametrize(
    "kwargs,error_type",
    [
        ({"max_duration": 0.05, "speech_wait_timeout": 1}, vad.RecordingTimeoutError),
        ({"max_duration": 1, "speech_wait_timeout": 0.05}, vad.NoSpeechError),
    ],
)
def test_recording_timeouts_close_stream(monkeypatch, kwargs, error_type):
    stream = install_stream(monkeypatch, [])

    with pytest.raises(error_type):
        vad.VoiceRecorder(**kwargs).record()

    assert stream.stopped and stream.closed


def test_cancel_while_waiting_closes_stream(monkeypatch):
    stream = install_stream(monkeypatch, [])
    cancel = threading.Event()
    recorder = vad.VoiceRecorder(max_duration=1, speech_wait_timeout=1)

    result = {}
    def run():
        try:
            recorder.record(cancel)
        except Exception as error:
            result["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.05)
    cancel.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert isinstance(result["error"], vad.RecordingCancelledError)
    assert stream.stopped and stream.closed


def test_cancel_during_active_recording_closes_stream(monkeypatch):
    stream = install_stream(monkeypatch, [audio_batch(0.02)])
    cancel = threading.Event()
    recorder = vad.VoiceRecorder(max_duration=1, speech_wait_timeout=1)

    result = {}

    def run():
        try:
            recorder.record(cancel)
        except Exception as error:
            result["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.05)
    cancel.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert isinstance(result["error"], vad.RecordingCancelledError)
    assert stream.stopped and stream.closed
