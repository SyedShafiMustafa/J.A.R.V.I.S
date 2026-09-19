"""agents/llm_provider.py

A small model-provider abstraction for the JARVIS brain and planner.

Two providers implement one interface:

- ``OllamaProvider``  — the existing local default (llama3.1:latest), unchanged
  request shape, retries, timeout, and error classes.
- ``OpenAICompatProvider`` — any OpenAI-compatible ``/chat/completions``
  endpoint (OpenAI, Groq, Together, a local vLLM/Ollama-compatible gateway,
  ...). This is the fast path when the CPU cannot keep a warm local model
  under the latency target.

Selection is configuration only (see ``config/settings.py``)::

    LLM_PROVIDER=ollama            # default, local
    LLM_PROVIDER=openai
    LLM_BASE_URL=https://api.example.com/v1
    LLM_API_KEY=...                # .env only, never hardcoded
    LLM_MODEL=gpt-4o-mini

Callers (``brain.py``, ``planner.py``) speak only to the ``LLMProvider``
interface, so no provider-specific logic spreads through the app. If the
cloud provider is requested but not fully configured, the factory falls back
to Ollama so JARVIS always has a working local brain.
"""

from __future__ import annotations

import json
import logging
from typing import Iterator

import requests

from config.config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_PROVIDER,
    LLM_TEMPERATURE,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT,
    OLLAMA_URL,
)
from agents.ollama_client import post_with_retries
from agents.ollama_errors import OllamaMalformedResponseError

_log = logging.getLogger("jarvis.llm")


def _sentences(tokens: Iterator[str]) -> Iterator[str]:
    """Buffer streamed tokens into complete sentences so TTS can start early."""
    buffer = ""
    for token in tokens:
        buffer += token
        while True:
            idx = max(buffer.rfind(". "), buffer.rfind("? "), buffer.rfind("! "))
            if idx == -1:
                break
            sentence = buffer[: idx + 1].strip()
            buffer = buffer[idx + 2:]
            if sentence:
                yield sentence
    if buffer.strip():
        yield buffer.strip()


class OllamaProvider:
    """Local Ollama chat provider — the existing JARVIS default."""

    name = "ollama"

    def __init__(self, url: str | None = None, model: str | None = None,
                 keep_alive: str | None = None) -> None:
        self.url = (url or OLLAMA_URL).replace("/generate", "/chat")
        self.model = model or OLLAMA_MODEL
        self.keep_alive = keep_alive or OLLAMA_KEEP_ALIVE

    def stream(self, messages: list[dict], **options) -> Iterator[str]:
        payload = {
            "model": self.model,
            "stream": True,
            "keep_alive": self.keep_alive,
            "messages": messages,
            "options": {
                "temperature": LLM_TEMPERATURE,
                "num_predict": 150,
                "num_ctx": 1024,
                **options,
            },
        }
        response = post_with_retries(self.url, json=payload, stream=True)

        def _tokens() -> Iterator[str]:
            try:
                for line in response.iter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line.decode())
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise OllamaMalformedResponseError("invalid Ollama stream data") from exc
                    token = data.get("message", {}).get("content", "")
                    if not isinstance(token, str):
                        raise OllamaMalformedResponseError("invalid Ollama token")
                    yield token
            except requests.Timeout as exc:
                from agents.ollama_errors import OllamaTimeoutError
                raise OllamaTimeoutError("Ollama stream timed out") from exc
            except requests.ConnectionError as exc:
                from agents.ollama_errors import OllamaUnavailableError
                raise OllamaUnavailableError("Ollama stream disconnected") from exc
            finally:
                response.close()

        return _sentences(_tokens())

    def complete(self, messages: list[dict], **options) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "messages": messages,
            "options": {"temperature": 0, **options},
        }
        response = post_with_retries(self.url, json=payload, stream=False)
        try:
            return response.json()["message"]["content"]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise OllamaMalformedResponseError("invalid Ollama response") from exc
        finally:
            response.close()


class OpenAICompatProvider:
    """Any OpenAI-compatible ``/chat/completions`` endpoint."""

    name = "openai"

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or LLM_BASE_URL).rstrip("/")
        self.api_key = api_key or LLM_API_KEY
        self.model = model or LLM_MODEL
        self.timeout = timeout or OLLAMA_TIMEOUT
        if not self.base_url or not self.api_key or not self.model:
            raise ValueError("OpenAI provider requires LLM_BASE_URL, LLM_API_KEY and LLM_MODEL")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def stream(self, messages: list[dict], **options) -> Iterator[str]:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": LLM_TEMPERATURE,
        }
        response = requests.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=self._headers(),
            stream=True,
            timeout=self.timeout,
        )
        response.raise_for_status()

        def _tokens() -> Iterator[str]:
            try:
                for line in response.iter_lines():
                    if not line:
                        continue
                    text = line.decode(errors="replace").strip()
                    if not text.startswith("data:"):
                        continue
                    data = text[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    token = delta.get("content")
                    if isinstance(token, str) and token:
                        yield token
            finally:
                response.close()

        return _sentences(_tokens())

    def complete(self, messages: list[dict], **options) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": 0,
        }
        response = requests.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        try:
            return response.json()["choices"][0]["message"]["content"]
        finally:
            response.close()


def build_llm_provider():
    """Return the configured provider; Ollama unless a cloud endpoint is set."""
    provider = (LLM_PROVIDER or "ollama").strip().lower()
    if provider in {"openai", "openai_compatible", "openai-compatible", "cloud"}:
        if LLM_BASE_URL and LLM_API_KEY and LLM_MODEL:
            _log.info("LLM provider: OpenAI-compatible (%s, %s)", LLM_BASE_URL, LLM_MODEL)
            return OpenAICompatProvider()
        _log.warning(
            "LLM_PROVIDER=%s but LLM_BASE_URL/LLM_API_KEY/LLM_MODEL is incomplete; "
            "falling back to local Ollama", provider,
        )
    _log.info("LLM provider: Ollama (%s)", OLLAMA_MODEL)
    return OllamaProvider()
