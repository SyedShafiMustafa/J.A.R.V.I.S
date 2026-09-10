import json
import requests

from config.config import OLLAMA_URL, OLLAMA_MODEL


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
            response = requests.post(
                self.url.replace("/generate", "/chat"),
                json=payload,
                stream=True
            )
            response.raise_for_status()
        except requests.RequestException as e:
            print(f"❌ Ollama error: {e}")
            yield "I'm having trouble reaching my brain. Please check that Ollama is running."
            return

        buffer = ""

        for line in response.iter_lines():

            if not line:
                continue

            try:
                data = json.loads(line.decode())
            except json.JSONDecodeError:
                continue

            token = data.get("message", {}).get("content", "")

            buffer += token

            while True:

                idx = max(
                    buffer.rfind(". "),
                    buffer.rfind("? "),
                    buffer.rfind("! ")
                )

                if idx == -1:
                    break

                sentence = buffer[:idx + 1].strip()
                buffer = buffer[idx + 2:]

                if sentence:
                    yield sentence

        if buffer.strip():
            yield buffer.strip()

    def ask(self, messages: list[dict]):
        return " ".join(self.stream(messages))