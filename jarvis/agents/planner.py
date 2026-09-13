import json

from config.config import OLLAMA_URL, OLLAMA_MODEL
from agents.ollama_client import post_with_retries
from agents.ollama_errors import OllamaError, OllamaMalformedResponseError, PlannerValidationError


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
            response = post_with_retries(self.url, json=payload, stream=False)
        except OllamaError:
            raise

        try:
            content = response.json()["message"]["content"].strip()
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise OllamaMalformedResponseError("invalid Ollama planner response") from exc
        finally:
            response.close()

        # Remove markdown if Ollama adds it
        if content.startswith("```"):
            try:
                content = content.split("\n", 1)[1]
                content = content.rsplit("```", 1)[0].strip()
            except IndexError as exc:
                raise PlannerValidationError("planner returned malformed markdown") from exc

        try:
            plan = json.loads(content)
        except json.JSONDecodeError as exc:
            raise PlannerValidationError("planner returned malformed JSON") from exc
        self._validate_plan(plan)
        return plan

    @staticmethod
    def _validate_plan(plan: object) -> None:
        if not isinstance(plan, dict):
            raise PlannerValidationError("planner output must be an object")
        if set(plan) - {"goal", "steps"}:
            raise PlannerValidationError("planner output contains unexpected fields")
        if not isinstance(plan.get("goal"), str) or not plan["goal"].strip():
            raise PlannerValidationError("planner goal must be a non-empty string")
        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            raise PlannerValidationError("planner steps must be a non-empty array")

        schemas = {
            "open_app": {"app": str},
            "wait_window": {"title": str},
            "click_text": {"text": str},
            "type": {"text": str},
            "press": {"key": str},
            "hotkey": {"keys": list},
            "close_app": {"app": str},
            "open_youtube": {},
            "search_youtube": {"query": str},
            "search_google": {"query": str},
        }
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
                raise PlannerValidationError("planner action must be an object with a tool")
            tool = step["tool"]
            if tool not in schemas:
                raise PlannerValidationError(f"planner returned invalid tool: {tool}")
            expected = schemas[tool]
            if set(step) - {"tool", *expected}:
                raise PlannerValidationError("planner action contains unexpected fields")
            if set(expected) - set(step):
                raise PlannerValidationError(f"planner action missing fields for {tool}")
            for field, field_type in expected.items():
                value = step[field]
                if field_type is list:
                    valid = (
                        isinstance(value, list)
                        and bool(value)
                        and all(isinstance(item, str) and item.strip() for item in value)
                    )
                else:
                    valid = isinstance(value, field_type) and bool(value.strip())
                if not valid:
                    raise PlannerValidationError(f"planner field {field} has an invalid type or value")