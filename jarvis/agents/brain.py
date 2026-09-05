import json
import requests

from config.config import OLLAMA_URL, OLLAMA_MODEL

SYSTEM_PROMPT = """
You are JARVIS, a fast desktop AI assistant.
Reply naturally in 1-2 sentences unless asked otherwise.
"""


class JarvisBrain:

    def __init__(self):
        self.url = OLLAMA_URL
        self.model = OLLAMA_MODEL

    def stream(self, prompt: str):

        payload = {
            "model": self.model,
            "stream": True,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            "options": {
                "temperature": 0.3,
                "num_predict": 40,
                "num_ctx": 1024
            }
        }

        response = requests.post(
            self.url.replace("/generate", "/chat"),
            json=payload,
            stream=True
        )

        response.raise_for_status()

        buffer = ""

        for line in response.iter_lines():

            if not line:
                continue

            data = json.loads(line.decode())

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

    def ask(self, prompt: str):
        return " ".join(self.stream(prompt))