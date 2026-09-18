import logging
import types
from agents.ollama_errors import OllamaUnavailableError
from backend.server import JarvisBackendService, _make_handler
from core.memory import Memory


def test_summary_unavailable_is_logged_and_non_fatal(monkeypatch, caplog):
    class Brain:
        def stream(self, messages):
            raise OllamaUnavailableError("offline")

    monkeypatch.setattr("agents.brain.JarvisBrain", Brain)
    caplog.set_level(logging.WARNING, logger="jarvis.memory")

    assert Memory._generate_summary(object(), "User: hello") is None
    assert "conversation summary unavailable" in caplog.text


def test_unexpected_summary_failure_is_logged_and_non_fatal(monkeypatch, caplog):
    class Brain:
        def stream(self, messages):
            raise ValueError("bad test response")

    monkeypatch.setattr("agents.brain.JarvisBrain", Brain)
    caplog.set_level(logging.ERROR, logger="jarvis.memory")

    assert Memory._generate_summary(object(), "User: hello") is None
    assert "conversation summary generation failed" in caplog.text


def test_optional_warmup_failure_is_logged_and_recoverable(monkeypatch, caplog):
    from backend.live_runtime import _warm_up_models

    class Audio:
        class stt:
            @staticmethod
            def transcribe(text):
                raise RuntimeError("stt unavailable")

        class tts:
            @staticmethod
            def speak(text):
                raise RuntimeError("tts unavailable")

            @staticmethod
            def wait():
                pass

    class Brain:
        def stream(self, messages):
            raise OllamaUnavailableError("offline")

    caplog.set_level(logging.WARNING, logger="jarvis.runtime")
    _warm_up_models(Audio(), Brain())
    assert "Whisper warm-up failed" in caplog.text
    assert "Piper warm-up failed" in caplog.text
    # The Ollama warm-up is fire-and-forget; wait for its thread to log.
    import time as _time
    limit = _time.monotonic() + 5
    while "Ollama warm-up failed" not in caplog.text and _time.monotonic() < limit:
        _time.sleep(0.05)
    assert "Ollama warm-up failed" in caplog.text


def test_malformed_websocket_message_is_logged_without_raising(monkeypatch, caplog):
    service = JarvisBackendService()
    handler_type = _make_handler(service)
    handler = handler_type.__new__(handler_type)
    caplog.set_level(logging.WARNING, logger="jarvis.backend")

    handler._ws_handle_text("{not json")

    assert "websocket client sent malformed JSON" in caplog.text


def test_websocket_disconnect_is_graceful(caplog):
    service = JarvisBackendService()
    handler_type = _make_handler(service)
    handler = handler_type.__new__(handler_type)
    handler.connection = types.SimpleNamespace(
        sendall=lambda payload: (_ for _ in ()).throw(ConnectionResetError())
    )
    caplog.set_level(logging.DEBUG, logger="jarvis.backend")

    handler._ws_send_frame(1, b"payload")

    assert "websocket client disconnected during send" in caplog.text
