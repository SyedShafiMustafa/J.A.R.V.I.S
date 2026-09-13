from __future__ import annotations

import logging
import time

import requests

from config.config import OLLAMA_MAX_RETRIES, OLLAMA_RETRY_DELAY, OLLAMA_TIMEOUT
from agents.ollama_errors import OllamaTimeoutError, OllamaUnavailableError

_log = logging.getLogger("jarvis.ollama")


def post_with_retries(url: str, *, json: dict, stream: bool):
    last_error: Exception | None = None
    for attempt in range(OLLAMA_MAX_RETRIES + 1):
        try:
            response = requests.post(
                url,
                json=json,
                stream=stream,
                timeout=OLLAMA_TIMEOUT,
            )
            response.raise_for_status()
            return response
        except requests.Timeout as exc:
            last_error = exc
            if attempt >= OLLAMA_MAX_RETRIES:
                raise OllamaTimeoutError("Ollama request timed out") from exc
        except requests.ConnectionError as exc:
            last_error = exc
            if attempt >= OLLAMA_MAX_RETRIES:
                raise OllamaUnavailableError("Ollama connection failed") from exc
        except requests.HTTPError as exc:
            last_error = exc
            status = exc.response.status_code if exc.response is not None else 0
            if status < 500 or attempt >= OLLAMA_MAX_RETRIES:
                raise OllamaUnavailableError(f"Ollama returned HTTP {status}") from exc
        except requests.RequestException as exc:
            raise OllamaUnavailableError("Ollama request failed") from exc

        delay = OLLAMA_RETRY_DELAY * (2 ** attempt)
        _log.warning("Ollama request failed; retrying in %.2fs", delay)
        time.sleep(delay)

    raise OllamaUnavailableError("Ollama request failed") from last_error
