import json

from config.config import OLLAMA_URL, OLLAMA_MODEL
from agents.ollama_client import post_with_retries
from agents.llm_provider import build_llm_provider
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

11. send_whatsapp (preferred for sending a WhatsApp message to a named contact)
{"tool":"send_whatsapp","recipient":"Ahmed","message":"I am reaching in 10 minutes"}

12. list_files
{"tool":"list_files","directory":"."}

13. inspect_file
{"tool":"inspect_file","path":"report.txt"}

14. create_file
{"tool":"create_file","path":"report.txt","content":"hello"}

15. edit_file
{"tool":"edit_file","path":"report.txt","content":"new contents"}

16. move_file
{"tool":"move_file","src":"a.txt","dst":"b.txt"}

17. search_files
{"tool":"search_files","directory":".","pattern":"*.py"}

18. delete_file
{"tool":"delete_file","path":"old.txt"}

19. execute_terminal
{"tool":"execute_terminal","command":"dir"}

20. git_status / git_diff / git_log / git_branch
{"tool":"git_status"}
{"tool":"git_diff"}
{"tool":"git_log","n":5}
{"tool":"git_branch"}

21. run_python_script
{"tool":"run_python_script","script_path":"scripts/foo.py"}

22. run_pytest
{"tool":"run_pytest","test_path":"core/tests"}

23. analyze_directory (summarize a folder without changing it)
{"tool":"analyze_directory","directory":"downloads"}

24. find_duplicates (content-hash duplicate detection, read-only)
{"tool":"find_duplicates","directory":"downloads"}

25. organize_directory (clean up a folder by type/date/year/extension/age)
{"tool":"organize_directory","directory":"downloads","strategy":"by_type","dry_run":true}

========================
RULES
========================

- Return ONLY JSON.
- For desktop apps use open_app then wait_window.
- Use click_text for visible UI elements.
- For ANY messaging application, use message_box instead of "Type a message".
- Preserve contact names exactly.
- Preserve message text exactly.
- To send a NEW WhatsApp message to a named contact, prefer the single
  send_whatsapp step over a click_text/type sequence.

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
Send Ahmed I am reaching in 10 minutes on WhatsApp

Response:
{
  "goal":"Send a WhatsApp message",
  "steps":[
    {"tool":"send_whatsapp","recipient":"Ahmed","message":"I am reaching in 10 minutes"}
  ]
}

User:
Create a file notes.txt with hello

Response:
{
  "goal":"Create notes.txt",
  "steps":[
    {"tool":"create_file","path":"notes.txt","content":"hello"}
  ]
}
"""


class TaskPlanner:

    def __init__(self):
        self.provider = build_llm_provider()
        self.url = getattr(self.provider, "url", None)
        self.model = getattr(self.provider, "model", None)

    def create_plan(self, request: str, lessons: list[str] | None = None):

        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            },
            {
                "role": "user",
                "content": request
            }
        ]

        # Bounded experience injection: at most a few retrieved lessons, each
        # truncated, so prior execution knowledge helps without flooding the
        # prompt (and never leaking the whole experience table).
        if lessons:
            bullets = "\n".join(
                f"- {str(lesson)[:200]}" for lesson in list(lessons)[:3]
            )
            if bullets:
                messages.insert(1, {
                    "role": "system",
                    "content": (
                        "Operational lessons from previous runs. Use a lesson ONLY "
                        "if it clearly applies to this request:\n" + bullets
                    ),
                })

        try:
            content = self.provider.complete(messages).strip()
        except OllamaError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise OllamaMalformedResponseError("invalid planner response") from exc

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

        # tool -> (required fields, optional fields). Optional fields may be
        # omitted but are still validated when present, so a planner step can
        # never smuggle in an unexpected field.
        schemas: dict[str, tuple[dict[str, type], dict[str, type]]] = {
            "open_app": ({"app": str}, {}),
            "wait_window": ({"title": str}, {}),
            "click_text": ({"text": str}, {}),
            "type": ({"text": str}, {}),
            "press": ({"key": str}, {}),
            "hotkey": ({"keys": list}, {}),
            "close_app": ({"app": str}, {}),
            "open_youtube": ({}, {}),
            "search_youtube": ({"query": str}, {}),
            "search_google": ({"query": str}, {}),
            "send_whatsapp": ({"recipient": str, "message": str}, {}),
            "list_files": ({}, {"directory": str}),
            "inspect_file": ({"path": str}, {}),
            "create_file": ({"path": str}, {"content": str}),
            "edit_file": ({"path": str, "content": str}, {}),
            "move_file": ({"src": str, "dst": str}, {}),
            "search_files": ({"directory": str, "pattern": str}, {}),
            "delete_file": ({"path": str}, {}),
            "execute_terminal": ({"command": str}, {"timeout": (int, float), "cwd": str}),
            "git_status": ({}, {}),
            "git_diff": ({}, {}),
            "git_log": ({}, {"n": int}),
            "git_branch": ({}, {}),
            "run_python_script": ({"script_path": str}, {"args": list}),
            "run_pytest": ({}, {"test_path": str}),
            "analyze_directory": ({}, {"directory": str, "recursive": bool}),
            "find_duplicates": ({}, {"directory": str, "recursive": bool}),
            "organize_directory": (
                {"directory": str},
                {"strategy": str, "dry_run": bool, "older_than_days": int, "recursive": bool},
            ),
        }
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
                raise PlannerValidationError("planner action must be an object with a tool")
            tool = step["tool"]
            if tool not in schemas:
                raise PlannerValidationError(f"planner returned invalid tool: {tool}")
            required, optional = schemas[tool]
            allowed = {**required, **optional}
            if set(step) - {"tool", *allowed}:
                raise PlannerValidationError("planner action contains unexpected fields")
            if set(required) - set(step):
                raise PlannerValidationError(f"planner action missing fields for {tool}")
            for field, field_type in allowed.items():
                if field not in step:
                    continue
                value = step[field]
                if field_type is list:
                    valid = (
                        isinstance(value, list)
                        and bool(value)
                        and all(isinstance(item, str) and item.strip() for item in value)
                    )
                elif field_type == (int, float):
                    valid = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
                elif field_type is bool:
                    valid = isinstance(value, bool)
                elif field_type is int:
                    valid = isinstance(value, int) and not isinstance(value, bool) and value > 0
                else:
                    valid = isinstance(value, field_type) and bool(value.strip())
                if not valid:
                    raise PlannerValidationError(f"planner field {field} has an invalid type or value")