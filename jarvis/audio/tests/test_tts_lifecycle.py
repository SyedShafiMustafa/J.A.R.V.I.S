import threading
import time
import types

import numpy as np

from audio import tts


class FakeStream:
    streams = []

    def __init__(self, **kwargs):
        self.started = False
        self.stopped = False
        self.closed = False
        self.writes = []
        self.__class__.streams.append(self)

    def start(self):
        self.started = True

    def write(self, audio):
        self.writes.append(audio)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeVoice:
    def synthesize(self, text):
        yield types.SimpleNamespace(audio_int16_array=np.ones(2205, dtype=np.int16))


def make_tts(monkeypatch):
    FakeStream.streams = []
    monkeypatch.setattr(tts.sd, "OutputStream", FakeStream)
    monkeypatch.setattr(tts.sd, "stop", lambda: None)
    engine = tts.TextToSpeech.__new__(tts.TextToSpeech)
    engine.voice = FakeVoice()
    engine.sample_rate = 22050
    engine.queue = __import__("queue").Queue()
    engine.running = True
    engine._current_stream = None
    engine._stop_event = threading.Event()
    engine._lock = threading.Lock()
    engine.worker = threading.Thread(target=engine._speaker_loop, name="test-tts-worker", daemon=True)
    engine.worker.start()
    return engine


def test_repeated_speech_and_order(monkeypatch):
    engine = make_tts(monkeypatch)
    engine.speak("one")
    engine.speak("two")
    engine.speak("three")
    engine.wait()
    assert len(FakeStream.streams) == 3
    assert all(stream.closed for stream in FakeStream.streams)
    engine.stop()


def test_stop_is_safe_idle_repeated_and_allows_new_speech(monkeypatch):
    engine = make_tts(monkeypatch)
    engine.stop_speaking()
    engine.stop_speaking()
    engine.speak("after stop")
    engine.wait()
    assert len(FakeStream.streams) == 1
    engine.stop()


def test_stop_during_playback_drains_queue_and_closes_stream(monkeypatch):
    gate = threading.Event()

    class BlockingStream(FakeStream):
        def write(self, audio):
            self.writes.append(audio)
            gate.wait(timeout=1)

    monkeypatch.setattr(tts.sd, "OutputStream", BlockingStream)
    engine = make_tts(monkeypatch)
    engine.speak("active")
    engine.speak("stale")
    deadline = time.monotonic() + 1
    while not FakeStream.streams and time.monotonic() < deadline:
        time.sleep(0.01)
    engine.stop_speaking()
    gate.set()
    engine.wait()
    assert FakeStream.streams[0].closed
    assert len(FakeStream.streams) == 1
    engine.stop()
