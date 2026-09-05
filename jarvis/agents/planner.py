import json
import requests

from config.config import OLLAMA_URL, OLLAMA_MODEL


SYSTEM_PROMPT = """
You are JARVIS's planning engine.

Convert the user's request into ONLY valid JSON.

Return JSON only.
No markdown.
No explanations.

========================
AVAILABLE TOOLS
========================

1. open_app
{"tool":"open_app","app":"whatsapp"}

2. wait_window
{"tool":"wait_window","title":"WhatsApp"}

3. click_text
{"tool":"click_text","text":"Search"}

Special semantic target:
{"tool":"click_text","text":"message_box"}

4. type
{"tool":"type","text":"Hello"}

5. press
{"tool":"press","key":"enter"}

6. hotkey
{"tool":"hotkey","keys":["ctrl","s"]}

7. close_app
{"tool":"close_app","app":"discord"}

8. open_youtube
{"tool":"open_youtube"}

9. search_youtube
{"tool":"search_youtube","query":"AI news"}

10. search_google
{"tool":"search_google","query":"GPT-5"}

========================
RULES
========================

- Return ONLY JSON.
- For desktop apps use open_app then wait_window.
- Use click_text for visible UI elements.
- For ANY messaging application, use message_box instead of "Type a message".
- Preserve contact names exactly.
- Preserve message text exactly.

========================
EXAMPLES
========================

User:
Open Notepad and write Hello Shafi

Response:
{
  "goal":"Write in Notepad",
  "steps":[
    {"tool":"open_app","app":"notepad"},
    {"tool":"wait_window","title":"Notepad"},
    {"tool":"type","text":"Hello Shafi"}
  ]
}

User:
Open VS Code

Response:
{
  "goal":"Open VS Code",
  "steps":[
    {"tool":"open_app","app":"vs code"},
    {"tool":"wait_window","title":"Visual Studio Code"}
  ]
}

User:
Open YouTube

Response:
{
  "goal":"Open YouTube",
  "steps":[
    {"tool":"open_youtube"}
  ]
}

User:
Search AI news on YouTube

Response:
{
  "goal":"Search YouTube",
  "steps":[
    {"tool":"search_youtube","query":"AI news"}
  ]
}

User:
Search GPT-5 on Google

Response:
{
  "goal":"Search Google",
  "steps":[
    {"tool":"search_google","query":"GPT-5"}
  ]
}

User:
Click Explorer

Response:
{
  "goal":"Click Explorer",
  "steps":[
    {"tool":"click_text","text":"Explorer"}
  ]
}

User:
Open WhatsApp and message Project Hello

Response:
{
  "goal":"Send a WhatsApp message",
  "steps":[
    {"tool":"open_app","app":"whatsapp"},
    {"tool":"wait_window","title":"WhatsApp"},
    {"tool":"click_text","text":"Search"},
    {"tool":"type","text":"Project"},
    {"tool":"press","key":"enter"},
    {"tool":"click_text","text":"message_box"},
    {"tool":"type","text":"Hello"},
    {"tool":"press","key":"enter"}
  ]
}
"""


class TaskPlanner:

    def __init__(self):
        self.url = OLLAMA_URL.replace("/generate", "/chat")
        self.model = OLLAMA_MODEL

    def create_plan(self, request: str):

        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": request
                }
            ],
            "options": {
                "temperature": 0
            }
        }

        try:
            response = requests.post(self.url, json=payload)
            response.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"Ollama not reachable ({e}) — is it running?")

        content = response.json()["message"]["content"].strip()

        # Remove markdown if Ollama adds it
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
            content = content.rsplit("```", 1)[0].strip()

        return json.loads(content)