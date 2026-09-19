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
import threading
from typing import Iterator

import requests

from config.config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_FALLBACK,
    LLM_MODEL,
    LLM_PROVIDER,
    LLM_TEMPERATURE,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT,
    OLLAMA_URL,
)
from agents.ollama_client import post_with_retries
from agents.ollama_errors import OllamaError, OllamaMalformedResponseError

_log = logging.getLogger("jarvis.llm")

# ---------------------------------------------------------------------------
# Active-provider status
#
# The UI shows which brain actually served the last request. Providers update
# this registry; it never contains secrets (no keys, no headers).
# ---------------------------------------------------------------------------

_STATUS_LOCK = threading.Lock()
_STATUS: dict = {
    "active": None,          # "openai" or "ollama" — who served the last call
    "primary": None,         # configured primary provider
    "fallback": None,        # configured fallback provider (or None)
    "vendor": None,          # friendly vendor label, e.g. "groq"/"ollama"
    "model": None,           # active model name
    "primary_model": None,
    "fallback_model": None,
    "fallback_used": False,  # True once the fallback has actually served a call
}


def _vendor(base_url: str | None, provider_name: str) -> str:
    """Friendly vendor label from the endpoint host — never the key or path."""
    host = (base_url or "").lower()
    if "groq" in host:
        return "groq"
    if host:
        return provider_name
    return "ollama"


def _set_status(**updates) -> None:
    with _STATUS_LOCK:
        _STATUS.update(updates)


def provider_status() -> dict:
    """Snapshot of the configured/active brain, safe for the UI (no secrets)."""
    with _STATUS_LOCK:
        return dict(_STATUS)


def configured_provider_status() -> dict:
    """Describe the configured brain without constructing a provider.

    Used at startup so the HUD can show the active brain before the first
    request has been made. Contains no secrets.
    """
    provider = (LLM_PROVIDER or "ollama").strip().lower()
    cloud = (
        provider in {"openai", "openai_compatible", "openai-compatible", "cloud"}
        and bool(LLM_BASE_URL and LLM_API_KEY and LLM_MODEL)
    )
    if cloud:
        return {
            "active": "openai",
            "primary": "openai",
            "fallback": "ollama" if LLM_FALLBACK else None,
            "vendor": _vendor(LLM_BASE_URL, "openai"),
            "model": LLM_MODEL,
            "primary_model": LLM_MODEL,
            "fallback_model": OLLAMA_MODEL,
            "fallback_used": False,
        }
    return {
        "active": "ollama",
        "primary": "ollama",
        "fallback": None,
        "vendor": "ollama",
        "model": OLLAMA_MODEL,
        "primary_model": OLLAMA_MODEL,
        "fallback_model": None,
        "fallback_used": False,
    }


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


# HTTP statuses that justify trying the local fallback (transient conditions).
# A rejected credential (401/403) or a bad request (400/404) does NOT fall
# back, so a misconfiguration surfaces instead of being masked.
_TRANSIENT_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _is_transient(exc: BaseException) -> bool:
    """True for unreachable / rate-limited / server errors worth retrying locally."""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in _TRANSIENT_HTTP_STATUS
    if isinstance(exc, OllamaError) and not isinstance(exc, OllamaMalformedResponseError):
        # The fallback target itself is down — nothing more to try.
        return True
    return False


class FallbackProvider:
    """Cloud primary with the local Ollama model as an automatic fallback.

    The fallback is used only for *transient* primary failures (unreachable,
    timed out, rate-limited, 5xx). A rejected credential or malformed request
    propagates to the caller instead of being silently hidden. If the primary
    fails mid-stream (after text was already spoken) the error propagates too,
    so a half-heard reply is never duplicated.
    """

    name = "fallback"

    def __init__(self, primary, fallback) -> None:
        self.primary = primary
        self.fallback = fallback
        self.url = getattr(primary, "url", None)
        self.base_url = getattr(primary, "base_url", None)
        self.model = getattr(primary, "model", None)
        self._primary_vendor = _vendor(self.base_url, primary.name)
        _set_status(
            active=primary.name,
            primary=primary.name,
            fallback=fallback.name,
            vendor=_vendor(getattr(primary, "base_url", None), primary.name),
            model=getattr(primary, "model", None),
            primary_model=getattr(primary, "model", None),
            fallback_model=getattr(fallback, "model", None),
            fallback_used=False,
        )

    def _activate(self, name: str, model, fallback_used: bool, vendor: str | None = None) -> None:
        _set_status(active=name, model=model, fallback_used=fallback_used, vendor=vendor)

    def _switch(self, exc: BaseException) -> None:
        _log.warning(
            "primary brain unavailable (%s); switching to local fallback %s",
            type(exc).__name__, self.fallback.name,
        )
        self._activate(self.fallback.name, getattr(self.fallback, "model", None), True, "ollama")

    def stream(self, messages: list[dict], **options) -> Iterator[str]:
        emitted = False
        try:
            for chunk in self.primary.stream(messages, **options):
                emitted = True
                yield chunk
            self._activate(self.primary.name, self.model, False, self._primary_vendor)
            return
        except Exception as exc:
            if not _is_transient(exc) or emitted:
                raise
            self._switch(exc)
        for chunk in self.fallback.stream(messages, **options):
            yield chunk

    def complete(self, messages: list[dict], **options) -> str:
        try:
            out = self.primary.complete(messages, **options)
            self._activate(self.primary.name, self.model, False, self._primary_vendor)
            return out
        except Exception as exc:
            if not _is_transient(exc):
                raise
            self._switch(exc)
        return self.fallback.complete(messages, **options)


def build_llm_provider():
    """Return the configured provider; Ollama unless a cloud endpoint is set."""
    provider = (LLM_PROVIDER or "ollama").strip().lower()
    if provider in {"openai", "openai_compatible", "openai-compatible", "cloud"}:
        if LLM_BASE_URL and LLM_API_KEY and LLM_MODEL:
            primary = OpenAICompatProvider()
            if LLM_FALLBACK:
                _log.info(
                    "LLM provider: %s primary (%s, %s) with %s fallback",
                    primary.name, LLM_BASE_URL, LLM_MODEL, OLLAMA_MODEL,
                )
                return FallbackProvider(primary, OllamaProvider())
            _log.info("LLM provider: OpenAI-compatible (%s, %s)", LLM_BASE_URL, LLM_MODEL)
            _set_status(
                active=primary.name, primary=primary.name, fallback=None,
                vendor=_vendor(LLM_BASE_URL, primary.name),
                model=primary.model, primary_model=primary.model,
                fallback_model=None, fallback_used=False,
            )
            return primary
        _log.warning(
            "LLM_PROVIDER=%s but LLM_BASE_URL/LLM_API_KEY/LLM_MODEL is incomplete; "
            "falling back to local Ollama", provider,
        )
    _log.info("LLM provider: Ollama (%s)", OLLAMA_MODEL)
    _set_status(
        active="ollama", primary="ollama", fallback=None, vendor="ollama",
        model=OLLAMA_MODEL, primary_model=OLLAMA_MODEL,
        fallback_model=None, fallback_used=False,
    )
    return OllamaProvider()
