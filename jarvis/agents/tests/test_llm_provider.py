import json

import pytest
import requests

from agents import llm_provider
from agents.llm_provider import (
    FallbackProvider,
    OllamaProvider,
    OpenAICompatProvider,
    build_llm_provider,
    configured_provider_status,
    provider_status,
)


class FakeResponse:
    def __init__(self, *, lines=None, payload=None):
        self.lines = lines or []
        self.payload = payload
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        return self.lines

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


def test_factory_defaults_to_ollama(monkeypatch):
    monkeypatch.setattr(llm_provider, "LLM_PROVIDER", "ollama")
    assert isinstance(build_llm_provider(), OllamaProvider)


def test_factory_falls_back_to_ollama_when_cloud_incomplete(monkeypatch):
    monkeypatch.setattr(llm_provider, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(llm_provider, "LLM_BASE_URL", "")
    monkeypatch.setattr(llm_provider, "LLM_API_KEY", "")
    monkeypatch.setattr(llm_provider, "LLM_MODEL", "")
    assert isinstance(build_llm_provider(), OllamaProvider)


def _configure_cloud(monkeypatch, *, fallback=True):
    monkeypatch.setattr(llm_provider, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(llm_provider, "LLM_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setattr(llm_provider, "LLM_API_KEY", "secret")
    monkeypatch.setattr(llm_provider, "LLM_MODEL", "openai/gpt-oss-20b")
    monkeypatch.setattr(llm_provider, "LLM_FALLBACK", fallback)


def test_factory_selects_openai_with_ollama_fallback(monkeypatch):
    _configure_cloud(monkeypatch)
    provider = build_llm_provider()
    assert isinstance(provider, FallbackProvider)
    assert isinstance(provider.primary, OpenAICompatProvider)
    assert isinstance(provider.fallback, OllamaProvider)
    assert provider.model == "openai/gpt-oss-20b"
    assert provider.base_url == "https://api.groq.com/openai/v1"


def test_factory_can_disable_fallback(monkeypatch):
    _configure_cloud(monkeypatch, fallback=False)
    provider = build_llm_provider()
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.model == "openai/gpt-oss-20b"


def test_groq_is_primary_and_ollama_is_the_configured_fallback(monkeypatch):
    _configure_cloud(monkeypatch)
    monkeypatch.setattr(llm_provider, "OLLAMA_MODEL", "llama3.1:latest")
    status = configured_provider_status()
    assert status["active"] == "openai"
    assert status["primary"] == "openai"
    assert status["fallback"] == "ollama"
    assert status["vendor"] == "groq"
    assert status["fallback_model"] == "llama3.1:latest"
    # Never leaks the key.
    assert "secret" not in json.dumps(status)


def test_status_tracks_active_provider_after_a_call(monkeypatch):
    _configure_cloud(monkeypatch)
    provider = build_llm_provider()
    response = FakeResponse(lines=[
        b'data: {"choices":[{"delta":{"content":"Hi."}}]}',
        b"data: [DONE]",
    ])
    monkeypatch.setattr("agents.llm_provider.requests.post", lambda *a, **k: response)
    assert list(provider.stream([{"role": "user", "content": "hi"}])) == ["Hi."]
    info = provider_status()
    assert info["active"] == "openai"
    assert info["vendor"] == "groq"
    assert info["fallback_used"] is False


class _DownPrimary:
    name = "openai"
    model = "openai/gpt-oss-20b"
    base_url = "https://api.groq.com/openai/v1"

    def stream(self, messages, **options):
        raise requests.ConnectionError("groq unreachable")
        yield  # pragma: no cover

    def complete(self, messages, **options):
        raise requests.ConnectionError("groq unreachable")


class _WorkingFallback:
    name = "ollama"
    model = "llama3.1:latest"

    def stream(self, messages, **options):
        yield "Local answer."

    def complete(self, messages, **options):
        return "Local answer."


class _RejectingPrimary(_DownPrimary):
    def stream(self, messages, **options):
        response = requests.Response()
        response.status_code = 401
        raise requests.HTTPError("unauthorized", response=response)
        yield  # pragma: no cover


def test_transient_primary_failure_falls_back_to_ollama():
    provider = FallbackProvider(_DownPrimary(), _WorkingFallback())
    assert list(provider.stream([{"role": "user", "content": "hi"}])) == ["Local answer."]
    info = provider_status()
    assert info["active"] == "ollama"
    assert info["fallback_used"] is True


def test_rejected_credential_does_not_silently_fall_back():
    provider = FallbackProvider(_RejectingPrimary(), _WorkingFallback())
    with pytest.raises(requests.HTTPError):
        list(provider.stream([{"role": "user", "content": "hi"}]))


def test_no_duplicate_fallback_after_partial_stream():
    class PartialPrimary(_DownPrimary):
        def stream(self, messages, **options):
            yield "First sentence."
            raise requests.ConnectionError("dropped mid-stream")

    provider = FallbackProvider(PartialPrimary(), _WorkingFallback())
    with pytest.raises(requests.ConnectionError):
        list(provider.stream([{"role": "user", "content": "hi"}]))


def test_openai_provider_streams_sentences_from_sse(monkeypatch):
    response = FakeResponse(lines=[
        b'data: {"choices":[{"delta":{"content":"Hello there. "}}]}',
        b'data: {"choices":[{"delta":{"content":"How are you?"}}]}',
        b"data: [DONE]",
    ])
    calls = []
    monkeypatch.setattr(
        "agents.llm_provider.requests.post",
        lambda *a, **k: calls.append(k) or response,
    )

    provider = OpenAICompatProvider(base_url="https://x/v1", api_key="k", model="m")
    result = list(provider.stream([{"role": "user", "content": "hi"}]))

    assert result == ["Hello there.", "How are you?"]
    assert response.closed
    # Authorization header must come from config, never hardcoded.
    assert calls[0]["headers"]["Authorization"] == "Bearer k"
    assert calls[0]["json"]["model"] == "m"
    assert calls[0]["json"]["stream"] is True


def test_ollama_provider_complete_parses_message_content(monkeypatch):
    response = FakeResponse(payload={"message": {"content": '{"goal": "g"}'}})
    monkeypatch.setattr("agents.ollama_client.requests.post", lambda *a, **k: response)
    out = OllamaProvider().complete([{"role": "user", "content": "x"}])
    assert out == '{"goal": "g"}'
    assert response.closed


def test_ollama_provider_stream_payload_uses_config_keep_alive(monkeypatch):
    response = FakeResponse(lines=[
        json.dumps({"message": {"content": "Done. "}}).encode(),
    ])
    calls = []
    monkeypatch.setattr(
        "agents.ollama_client.requests.post",
        lambda *a, **k: calls.append(k) or response,
    )
    provider = OllamaProvider(keep_alive="7m")
    assert list(provider.stream([{"role": "user", "content": "hi"}])) == ["Done."]
    assert calls[0]["json"]["keep_alive"] == "7m"
    assert calls[0]["timeout"] == 30.0
