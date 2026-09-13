import json
import requests

from config.config import OLLAMA_URL, OLLAMA_MODEL
from agents.ollama_client import post_with_retries
from agents.ollama_errors import OllamaMalformedResponseError, OllamaError


class JarvisBrain:

    def __init__(self):
        self.url = OLLAMA_URL
        self.model = OLLAMA_MODEL

    def stream(self, messages: list[dict]):

        payload = {
            "model": self.model,
            "stream": True,
            "keep_alive": "30m",
            "messages": messages,
            "options": {
                "temperature": 0.3,
                "num_predict": 150,
                "num_ctx": 1024
            }
        }

        try:
            response = post_with_retries(
                self.url.replace("/generate", "/chat"),
                json=payload,
                stream=True,
            )
        except OllamaError:
            raise

        buffer = ""
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
                buffer += token
                while True:
                    idx = max(buffer.rfind(". "), buffer.rfind("? "), buffer.rfind("! "))
                    if idx == -1:
                        break
                    sentence = buffer[:idx + 1].strip()
                    buffer = buffer[idx + 2:]
                    if sentence:
                        yield sentence
            if buffer.strip():
                yield buffer.strip()
        except requests.Timeout as exc:
            from agents.ollama_errors import OllamaTimeoutError
            raise OllamaTimeoutError("Ollama stream timed out") from exc
        except requests.ConnectionError as exc:
            from agents.ollama_errors import OllamaUnavailableError
            raise OllamaUnavailableError("Ollama stream disconnected") from exc
        finally:
            response.close()

    def ask(self, messages: list[dict]):
        return " ".join(self.stream(messages))