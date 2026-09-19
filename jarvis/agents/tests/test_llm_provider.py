import json

from agents import llm_provider
from agents.llm_provider import (
    OllamaProvider,
    OpenAICompatProvider,
    build_llm_provider,
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


def test_factory_selects_openai_when_fully_configured(monkeypatch):
    monkeypatch.setattr(llm_provider, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(llm_provider, "LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setattr(llm_provider, "LLM_API_KEY", "secret")
    monkeypatch.setattr(llm_provider, "LLM_MODEL", "gpt-fast")
    provider = build_llm_provider()
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.model == "gpt-fast"
    assert provider.base_url == "https://api.example.com/v1"


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
