import threading
import time
import types

from audio import wake_word
from backend.bus import BackendBus, BackendEvent
from backend.live_adapters import LiveAudioProvider
from backend.server import JarvisBackendService, STATUS_ERROR


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
