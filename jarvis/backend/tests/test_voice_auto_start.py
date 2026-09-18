"""Focused tests for automatic wake listener startup.

Covers:
- wake listener automatically starts via ensure_listening (exactly once)
- duplicate starts are prevented (concurrent and sequential)
- failure to start is reported, not swallowed
- service stops cleanly after auto-start
"""

import threading
import types

from backend.bus import BackendBus
from backend.server import JarvisBackendService


class StubAudio:
    def __init__(self, bus):
        self.bus = bus
        self.started = threading.Event()
        self.start_calls = 0
        self.stop_calls = 0

    def start_wake_word(self):
        self.start_calls += 1
        self.started.set()

    def stop_wake_word(self):
        self.stop_calls += 1


def make_service():
    bus = BackendBus()
    audio = StubAudio(bus)
    runtime = {
        "audio": audio,
        "bus": bus,
        "lifecycle": types.SimpleNamespace(shutdown_requested=False),
    }
    service = JarvisBackendService(runtime_builder=lambda **_: runtime)
    return service, audio


def test_ensure_listening_starts_voice_loop_once():
    service, audio = make_service()

    ok, error = service.ensure_listening()

    assert ok is True
    assert error is None
    assert service._voice_mode is True
    assert audio.started.wait(timeout=2.0)
    assert audio.start_calls == 1
    service.stop_listening()


def test_ensure_listening_is_idempotent():
    service, audio = make_service()
    service.ensure_listening()
    thread = service._voice_thread

    ok, error = service.ensure_listening()

    assert ok is True
    assert error is None
    assert service._voice_thread is thread
    assert audio.start_calls == 1
    service.stop_listening()


def test_concurrent_ensure_listening_starts_one_loop():
    service, audio = make_service()
    results = []

    def call():
        results.append(service.ensure_listening())

    threads = [threading.Thread(target=call) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(ok for ok, _ in results)
    assert audio.start_calls == 1
    service.stop_listening()


def test_ensure_listening_reports_failure(monkeypatch):
    service, _ = make_service()
    monkeypatch.setattr(service, "start_listening", lambda: (500, {"error": "boom"}))

    ok, error = service.ensure_listening()

    assert ok is False
    assert "boom" in error


def test_stop_after_auto_start_is_clean():
    service, audio = make_service()
    service.ensure_listening()
    assert audio.started.is_set()

    status, payload = service.stop_listening()

    assert status == 200
    assert payload == {"stopped": True}
    assert service._voice_mode is False
    assert audio.stop_calls >= 1
