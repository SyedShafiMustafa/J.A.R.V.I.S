import threading
import time
import types

import numpy as np
from audio import wake_word
from audio.vad import NoSpeechError
from backend.bus import BackendBus, BackendEvent
from backend.live_adapters import LiveAudioProvider
from backend.server import (
    JarvisBackendService,
    PHASE_CAPTURING,
    PHASE_ERROR,
    PHASE_EXECUTING,
    PHASE_IDLE,
    PHASE_SPEAKING,
    PHASE_STOPPING,
    PHASE_THINKING,
    PHASE_TRANSCRIBING,
    PHASE_WAKE_DETECTED,
    PHASE_WAKE_LISTENING,
    STATUS_ERROR,
)


class FakeDetector:
    instances = []

    def __init__(self, on_detect, on_error=None, on_started=None):
        self.on_detect = on_detect
        self.on_error = on_error
        self.on_started = on_started
        self.stop_event = threading.Event()
        self.stopped = False
        self.started = threading.Event()
        self.finished = threading.Event()
        self.__class__.instances.append(self)

    def start(self):
        self.started.set()
        if self.on_started:
            self.on_started()
        self.stop_event.wait()
        self.finished.set()

    def stop(self):
        self.stopped = True
        self.stop_event.set()


class FailingDetector(FakeDetector):
    def start(self):
        self.started.set()
        if self.on_started:
            self.on_started()
        error = RuntimeError("fake microphone failure")
        if self.on_error:
            self.on_error(error)
        raise error


class UnreportedFailingDetector(FakeDetector):
    def start(self):
        self.started.set()
        raise RuntimeError("unreported detector failure")


def make_provider(monkeypatch, detector_type=FakeDetector):
    FakeDetector.instances = []
    module = types.SimpleNamespace(WakeWordDetector=detector_type)
    monkeypatch.setitem(__import__("sys").modules, "audio.wake_word", module)
    provider = LiveAudioProvider.__new__(LiveAudioProvider)
    provider.bus = BackendBus()
    provider.recorder = None
    provider.stt = None
    provider.tts = None
    provider._wake_detector = None
    provider._wake_thread = None
    provider._wake_lock = threading.Lock()
    provider._session_id = "test-session"
    provider._speaking = False
    provider._barge_in_thread = None
    provider._barge_in_stop = threading.Event()
    return provider


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_start_duplicate_stop_and_restart(monkeypatch):
    provider = make_provider(monkeypatch)

    provider.start_wake_word()
    assert wait_until(lambda: FakeDetector.instances[0].started.is_set())
    first_detector = FakeDetector.instances[0]
    first_thread = provider._wake_thread

    provider.start_wake_word()
    assert len(FakeDetector.instances) == 1
    assert provider._wake_thread is first_thread

    provider.stop_wake_word()
    assert first_detector.stopped
    assert first_detector.finished.is_set()
    assert provider._wake_detector is None
    assert provider._wake_thread is None
    provider.stop_wake_word()

    provider.start_wake_word()
    assert wait_until(lambda: len(FakeDetector.instances) == 2)
    assert provider._wake_thread is not first_thread
    provider.stop_wake_word()


def test_detector_stop_closes_input_stream(monkeypatch):
    class FakeStream:
        def __init__(self):
            self.started = threading.Event()
            self.stopped = False
            self.closed = False

        def start(self):
            self.started.set()

        def stop(self):
            self.stopped = True

        def close(self):
            self.closed = True

    stream = FakeStream()
    monkeypatch.setattr(wake_word, "Model", lambda **_: object())
    monkeypatch.setattr(wake_word.sd, "InputStream", lambda **_: stream)
    detector = wake_word.WakeWordDetector(lambda: None)
    thread = threading.Thread(target=detector.start)
    thread.start()

    assert wait_until(stream.started.is_set)
    detector.stop()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert stream.stopped
    assert stream.closed


def test_detector_callback_failure_reports_once_and_stops(monkeypatch):
    class FakeStream:
        callback = None

        def __init__(self, **kwargs):
            self.__class__.callback = kwargs["callback"]
            self.stopped = False
            self.closed = False

        def start(self):
            self.__class__.callback(np.zeros((8, 1), dtype=np.float32), 8, None, None)

        def stop(self):
            self.stopped = True

        def close(self):
            self.closed = True

    errors = []
    monkeypatch.setattr(wake_word, "Model", lambda **_: types.SimpleNamespace(
        predict=lambda audio: (_ for _ in ()).throw(RuntimeError("predict failed"))
    ))
    monkeypatch.setattr(wake_word.sd, "InputStream", FakeStream)
    detector = wake_word.WakeWordDetector(lambda: None, on_error=errors.append)

    detector.start()

    assert len(errors) == 1
    assert str(errors[0]) == "predict failed"


def test_listener_failure_cleans_resources_and_publishes_error(monkeypatch):
    provider = make_provider(monkeypatch, FailingDetector)
    events = []
    provider.bus.subscribe(events.append)

    provider.start_wake_word()

    assert wait_until(lambda: any(event.kind == "wake.error" for event in events))
    assert wait_until(lambda: provider._wake_detector is None)
    assert provider._wake_thread is None
    assert not any(
        thread.name == "jarvis-wake-listener" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_unreported_listener_failure_publishes_one_error(monkeypatch):
    provider = make_provider(monkeypatch, UnreportedFailingDetector)
    events = []
    provider.bus.subscribe(events.append)

    provider.start_wake_word()

    assert wait_until(lambda: any(event.kind == "wake.error" for event in events))
    assert len([event for event in events if event.kind == "wake.error"]) == 1


class FakeAudio:
    def __init__(self):
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.stop_calls = 0

    def start_wake_word(self):
        self.started.set()

    def stop_wake_word(self):
        self.stop_calls += 1
        self.stopped.set()


class FailingAudio(FakeAudio):
    def __init__(self, bus):
        super().__init__()
        self.bus = bus

    def start_wake_word(self):
        self.started.set()
        self.bus.publish(BackendEvent(
            kind="wake.error",
            meta={"error": "fake microphone failure"},
        ))


class FakeLifecycle:
    shutdown_requested = False


class NoSpeechAudio:
    def __init__(self):
        self.transcribe_calls = 0

    def speak(self, text):
        pass

    def wait(self):
        pass

    def record_audio(self, cancel_event=None):
        raise NoSpeechError("no speech")

    def transcribe(self, path):
        self.transcribe_calls += 1
        return "should not be used"


class FakeSession:
    id = "test-session"
    conversation_id = None


class FakeMemory:
    def save_message(self, *args):
        pass


def test_backend_shutdown_stops_active_listener():
    audio = FakeAudio()
    runtime = {
        "audio": audio,
        "bus": BackendBus(),
        "lifecycle": FakeLifecycle(),
    }
    service = JarvisBackendService(runtime_builder=lambda **_: runtime)

    status, payload = service.start_listening()
    assert status == 200
    assert payload == {"started": True}
    assert wait_until(audio.started.is_set)

    status, payload = service.stop_listening()
    assert status == 200
    assert payload == {"stopped": True}
    assert audio.stop_calls >= 1
    assert service._voice_thread is None
    assert service.state.snapshot()["status"] != STATUS_ERROR


def test_backend_listener_failure_sets_error_state():
    bus = BackendBus()
    audio = FailingAudio(bus)
    runtime = {"audio": audio, "bus": bus, "lifecycle": FakeLifecycle()}
    service = JarvisBackendService(runtime_builder=lambda **_: runtime)

    status, _ = service.start_listening()
    assert status == 200
    assert wait_until(lambda: service._voice_thread is None)
    assert service.state.snapshot()["status"] == STATUS_ERROR
    assert service.state.snapshot()["error_message"] == "fake microphone failure"


def test_no_speech_returns_to_wake_without_stt():
    audio = NoSpeechAudio()
    service = JarvisBackendService()
    service._voice_mode = True
    service._voice_future = threading.Event()
    runtime = {
        "audio": audio,
        "session": FakeSession(),
        "lifecycle": FakeLifecycle(),
        "orchestrator": None,
        "bus": BackendBus(),
        "memory": FakeMemory(),
    }

    service._voice_conversation(runtime)

    assert audio.transcribe_calls == 0
    assert service.state.snapshot()["phase"] == PHASE_WAKE_LISTENING


def test_authoritative_phase_transitions_and_state_snapshot():
    service = JarvisBackendService()
    phases = [
        PHASE_IDLE,
        PHASE_WAKE_LISTENING,
        PHASE_WAKE_DETECTED,
        PHASE_CAPTURING,
        PHASE_TRANSCRIBING,
        PHASE_THINKING,
        PHASE_EXECUTING,
        PHASE_SPEAKING,
        PHASE_STOPPING,
        PHASE_IDLE,
    ]
    for phase in phases:
        service._set_phase(phase)
        assert service.state.snapshot()["phase"] == phase

    service.state.set_error("phase test failure")
    assert service.state.snapshot()["phase"] == PHASE_ERROR


def test_state_response_is_websocket_reconnect_snapshot():
    service = JarvisBackendService()
    service._set_phase(PHASE_TRANSCRIBING)

    status, snapshot = service._state_response()

    assert status == 200
    assert snapshot["phase"] == PHASE_TRANSCRIBING


class FakeBargeStream:
    streams = []

    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]
        self.started = False
        self.stopped = False
        self.closed = False
        self.__class__.streams.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeTts:
    def __init__(self):
        self.speaks = []
        self.stops = 0

    def speak(self, text):
        self.speaks.append(text)

    def wait(self):
        pass

    def stop_speaking(self):
        self.stops += 1


def make_audio_provider(monkeypatch):
    FakeBargeStream.streams = []
    monkeypatch.setattr("sounddevice.InputStream", FakeBargeStream)
    provider = LiveAudioProvider.__new__(LiveAudioProvider)
    provider.bus = BackendBus()
    provider.tts = FakeTts()
    provider._speaking = False
    provider._barge_in_thread = None
    provider._barge_in_stop = threading.Event()
    provider._barge_in_stream = None
    provider._barge_in_lock = threading.Lock()
    provider._barge_in_session = 0
    provider._session_id = "test-session"
    return provider


def test_barge_in_listener_is_single_and_stops_cleanly(monkeypatch):
    provider = make_audio_provider(monkeypatch)
    provider.speak("first")
    first_thread = provider._barge_in_thread
    provider.speak("second")
    assert provider._barge_in_thread is not first_thread
    provider.stop_speaking()
    provider.stop_speaking()
    assert provider._barge_in_thread is None
    assert all(stream.stopped and stream.closed for stream in FakeBargeStream.streams)


def test_barge_in_interrupts_active_session_once(monkeypatch):
    provider = make_audio_provider(monkeypatch)
    events = []
    provider.bus.subscribe(events.append)
    provider.speak("active")
    stream = FakeBargeStream.streams[-1]
    stream.callback(np.full((32, 1), 0.02, dtype=np.float32), 32, None, None)
    for _ in range(6):
        stream.callback(np.zeros((32, 1), dtype=np.float32), 32, None, None)
    assert wait_until(lambda: len([e for e in events if e.kind == "user.interrupt"]) == 1)
    provider.stop_speaking()
    assert len([e for e in events if e.kind == "user.interrupt"]) == 1


def test_barge_in_low_volume_does_not_interrupt(monkeypatch):
    provider = make_audio_provider(monkeypatch)
    events = []
    provider.bus.subscribe(events.append)
    provider.speak("quiet")
    stream = FakeBargeStream.streams[-1]
    for _ in range(10):
        stream.callback(np.zeros((32, 1), dtype=np.float32), 32, None, None)
    time.sleep(0.05)
    assert not any(e.kind == "user.interrupt" for e in events)
    provider.stop_speaking()
