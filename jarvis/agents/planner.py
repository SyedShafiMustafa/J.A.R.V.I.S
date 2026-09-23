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

26. screenshot (observe the screen: active window, full screen, or a region)
{"tool":"screenshot","mode":"active"}

27. inspect_screen (understand the screen: window, app, UI elements)
{"tool":"inspect_screen","mode":"active"}

28. locate_target (find a UI target by language; refuses when uncertain)
{"tool":"locate_target","target":"the Save button"}

29. visual_click (locate, click, re-observe and verify)
{"tool":"visual_click","target":"the Save button","button":"left"}

30. visual_type (focus a field when given, type, re-observe and verify)
{"tool":"visual_type","target":"the search box","text":"Python"}

31. visual_drag (drag from one located target onto another)
{"tool":"visual_drag","target":"the file","to_target":"the folder"}

32. visual_scroll (scroll the wheel; negative scrolls down)
{"tool":"visual_scroll","amount":-480}

33. visual_verify (re-observe and check an expected visual state)
{"tool":"visual_verify","kind":"text_visible","text":"Hello"}

34. visual_menu (open/close a menu-bar menu, verified by observation)
{"tool":"visual_menu","target":"the File menu","action":"open"}

35. list_windows (enumerate windows with titles, apps and geometry)
{"tool":"list_windows","pattern":"Chrome"}

36. switch_app (focus an application window, verified by handle)
{"tool":"switch_app","target":"Chrome"}

37. window_manage (minimize/maximize/restore/move/snap/check/close)
{"tool":"window_manage","target":"Notepad","action":"maximize"}

38. extract_window_text (OCR read-only, or clipboard for exact text)
{"tool":"extract_window_text","target":"Notepad","method":"ocr"}

39. read_clipboard (read clipboard text for handoff verification)
{"tool":"read_clipboard"}

 40. run_workflow (verified cross-app tool sequence with data handoff)
{"tool":"run_workflow","goal":"Copy notes to clipboard","steps":[{"tool":"switch_app","target":"Notepad"},{"tool":"extract_window_text","method":"clipboard","save_as":"notes"}]}

 41. get_setting (read a Windows setting without changing it)
{"tool":"get_setting","setting":"dark_mode"}
{"tool":"get_setting","setting":"system_volume"}

 42. set_setting (change a Windows setting, verified by re-reading)
{"tool":"set_setting","setting":"dark_mode","value":"on"}
{"tool":"set_setting","setting":"system_volume","value":50}
{"tool":"set_setting","setting":"system_volume","value":"mute"}

========================
RULES
========================

- Return ONLY JSON.
- You are an OPERATING agent, not a tutorial bot: ALWAYS answer with
  tool JSON that DOES the request. NEVER reply with instructions, user
  steps, or how-to text — there is no text channel and any non-JSON is
  a failure. "Maximize Chrome." means
  {"tool":"window_manage","target":"Chrome","action":"maximize"} —
  NEVER "To maximize Chrome, click...".
- For desktop apps use open_app then wait_window.
- Use click_text for visible UI elements.
- Prefer a reliable native/direct tool (open_app, type, press, click_text,
  send_whatsapp) when one exists; use the visual_* tools when no direct
  tool fits or the outcome must be visually verified.
- Never invent screen coordinates: visual tools locate targets by language.
- For window control use list_windows/switch_app/window_manage: never
  invent application names, and never act when a target is ambiguous.
- Hybrid priority: native window/API control first, app-specific tools
  second, OCR/UIA third, visual clicking last.
- For multi-step cross-app tasks prefer one run_workflow with per-step
  verification over loose tool sequences.
- Menu-bar items (File, Edit, View, ...) are NEVER open_app: "open the
  File menu" means visual_menu open on "the File menu" in the current
  window, never launching anything. The ONLY "file" exception is the
  application itself: "open File Explorer" means open_app file explorer.
- "This file" / "report.pdf" / document names mean inspect_file and
  friends, never open_app.
- Clipboard verbs are hotkeys, never messaging targets: "copy" means
  hotkey ctrl+c, "paste" means hotkey ctrl+v, "select all" means
  hotkey ctrl+a, "cut" means hotkey ctrl+x. There is no message_box
  outside a messaging application. After typing or pasting into a
  document, verify with visual_verify text_visible on the typed text.
- "Put X on the left/right/top/bottom" ALWAYS means window_manage
  snap_left/snap_right/snap_top/snap_bottom on X — never locate,
  click, or drag. "Maximize/minimize/restore X" ALWAYS means
  window_manage with that action on X.
- NEVER hedge with list_windows when the user named a target: emit
  the window_manage/switch_app step directly. If several windows
  match, the tool itself asks which one — a listing turn that acts on
  nothing is a failure to operate.
- "Close <app>" (whole program) means close_app; minimize/maximize/
  restore/snap and single-window close mean window_manage; bringing a
  window forward means switch_app or wait_window.
- For ANY messaging application, use message_box instead of "Type a message".
- Preserve contact names exactly.
- Preserve message text exactly.
- Windows settings use ONLY get_setting/set_setting with
  setting dark_mode (value "on"/"off") or system_volume (value
  0-100, "mute", "unmute", "up" or "down"). Read with get_setting
  ("Is dark mode on?", "What is my volume?"); change with
  set_setting ("Turn on dark mode.", "Set volume to 50."). These
  tools are ONLY for explicit OS-setting requests — never for chat
  that merely mentions darkness, sound or music. "How do I turn on
  dark mode?" is a how-to question and never reaches you as an
  action.
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


def _valid_setting_value(setting: object, value: object) -> bool:
    """Validate a ``set_setting`` value for its setting (§29).

    Mirrors the executor contract: dark_mode takes on/off words or a
    bool; system_volume takes 0-100, mute/unmute/up/down words (an
    optional trailing % is allowed). Anything else fails loudly so the
    feedback retry corrects it instead of executing garbage.
    """
    if setting == "dark_mode":
        if isinstance(value, bool):
            return True
        return isinstance(value, str) and value.strip().lower() in (
            "on", "off", "dark", "light", "enable", "disable",
            "enabled", "disabled", "true", "false", "1", "0")
    if setting == "system_volume":
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return 0 <= int(value) <= 100
        if not isinstance(value, str):
            return False
        low = value.strip().lower().rstrip("%")
        if low in ("mute", "muted", "unmute", "unmuted", "un-mute",
                   "up", "down", "louder", "quieter", "increase",
                   "decrease", "lower"):
            return True
        return low.isdigit() and 0 <= int(low) <= 100
    return False


def _valid_nested_steps(value: object, schemas: dict) -> bool:
    """Validate ``run_workflow`` nested steps against tool schemas."""
    if not isinstance(value, list) or not value or len(value) > 50:
        return False
    for nested in value:
        if not isinstance(nested, dict):
            return False
        tool = nested.get("tool")
        if not isinstance(tool, str) or tool not in schemas:
            return False
        # run_workflow cannot nest (no recursive plans).
        if tool == "run_workflow":
            return False
        required, optional = schemas[tool]
        allowed = {**required, **optional}
        # Workflow-control keys (handoff, postconditions, recovery
        # context) are validated by the workflow engine, not here.
        # Unknown noise keys are stripped like top-level steps.
        for k in [k for k in nested
                  if k not in ("tool", "save_as", "expect", "app",
                               *allowed)]:
            del nested[k]
        if set(required) - set(nested):
            return False
        for field, field_type in allowed.items():
            if field not in nested:
                continue
            item = nested[field]
            if field_type is bool:
                if not isinstance(item, bool):
                    return False
            elif field_type is int:
                if not isinstance(item, int) or isinstance(item, bool):
                    return False
            elif field_type == (int, float):
                if not isinstance(item, (int, float)) \
                        or isinstance(item, bool):
                    return False
            elif field_type is list:
                if not isinstance(item, list):
                    return False
            elif field_type is dict:
                if not isinstance(item, dict):
                    return False
            elif not isinstance(item, str):
                return False
    return True


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
        try:
            self._validate_plan(plan)
        except PlannerValidationError as exc:
            # One bounded recovery: small local models often add a stray
            # key or mistype one field while the intent is right. Feed
            # the exact complaint back once instead of failing the turn.
            plan = self._retry_with_feedback(messages, str(exc))
            self._validate_plan(plan)
        return plan

    def _retry_with_feedback(self, messages: list, error: str):
        """Re-ask once, pointing at the validation failure."""
        from agents.ollama_errors import (
            OllamaError, OllamaMalformedResponseError,
        )
        retry_messages = list(messages) + [{
            "role": "user",
            "content": (
                "Your last output failed validation: "
                f"{error}. Return ONLY the corrected JSON plan, "
                "no other text."
            ),
        }]
        try:
            content = self.provider.complete(retry_messages).strip()
        except OllamaError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise OllamaMalformedResponseError(
                "invalid planner response") from exc
        if content.startswith("```"):
            try:
                content = content.split("\n", 1)[1]
                content = content.rsplit("```", 1)[0].strip()
            except IndexError as exc:
                raise PlannerValidationError(
                    "planner returned malformed markdown") from exc
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise PlannerValidationError(
                "planner returned malformed JSON") from exc

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
            "screenshot": ({}, {"mode": str, "region": dict}),
            "inspect_screen": ({}, {"mode": str}),
            "locate_target": ({"target": str}, {"min_confidence": (int, float)}),
            "visual_click": (
                {"target": str},
                {"button": str, "min_confidence": (int, float),
                 "expected_window": str, "verify_text": str},
            ),
            "visual_type": (
                {"text": str},
                {"target": str, "min_confidence": (int, float),
                 "expected_window": str, "verify_text": str},
            ),
            "visual_drag": (
                {"target": str, "to_target": str},
                {"min_confidence": (int, float)},
            ),
            "visual_scroll": ({"amount": int}, {}),
            "visual_verify": (
                {"kind": str},
                {"text": str, "title": str},
            ),
            "visual_menu": (
                {"target": str},
                {"action": str, "min_confidence": (int, float)},
            ),
            "list_windows": ({}, {"pattern": str}),
            "switch_app": ({"target": str}, {}),
            "window_manage": (
                {"target": str, "action": str},
                {"x": int, "y": int, "width": int, "height": int,
                 "app": str},
            ),
            "extract_window_text": ({}, {"target": str, "method": str}),
            "read_clipboard": ({}, {}),
            "run_workflow": (
                {"goal": str, "steps": list},
                {"max_retries": int},
            ),
            "get_setting": ({"setting": str}, {}),
            "set_setting": (
                {"setting": str, "value": object},
                {"fallback_allowed": bool},
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
            # Noise tolerance for chatty small models: a JSON null on an
            # OPTIONAL field means "omitted" (standard JSON semantics),
            # and unknown extra keys are stripped. Unknown TOOLS, missing
            # REQUIRED fields and mistyped values still fail loudly —
            # stripping never invents or weakens a required contract.
            # (This mutates the parsed plan into its clean form.)
            for key in [k for k in step if k not in ("tool", *allowed)]:
                del step[key]
            for key in [k for k in optional if step.get(k) is None
                        and k in step]:
                del step[key]
            if set(required) - set(step):
                raise PlannerValidationError(f"planner action missing fields for {tool}")
            for field, field_type in allowed.items():
                if field not in step:
                    continue
                value = step[field]
                if tool == "visual_scroll" and field == "amount":
                    # Wheel direction is signed: positive scrolls up,
                    # negative scrolls down; zero is a harmless no-op.
                    valid = (
                        isinstance(value, int) and not isinstance(value, bool)
                    )
                elif tool == "window_manage" and field in ("x", "y"):
                    # Screen origin (0, 0) is a valid position.
                    valid = (
                        isinstance(value, int)
                        and not isinstance(value, bool) and value >= 0
                    )
                elif tool == "window_manage" and field in ("width", "height"):
                    valid = (
                        isinstance(value, int)
                        and not isinstance(value, bool) and value > 0
                    )
                elif tool == "run_workflow" and field == "max_retries":
                    # Zero retries (single attempt) is valid.
                    valid = (
                        isinstance(value, int)
                        and not isinstance(value, bool) and value >= 0
                    )
                elif tool == "run_workflow" and field == "steps":
                    # Nested plan steps name real tools with their
                    # required fields, so a bad workflow fails here
                    # with a clear error instead of mid-execution.
                    valid = _valid_nested_steps(value, schemas)
                elif tool == "get_setting" and field == "setting":
                    valid = value in ("dark_mode", "system_volume")
                elif tool == "set_setting" and field == "setting":
                    valid = value in ("dark_mode", "system_volume")
                elif tool == "set_setting" and field == "value":
                    valid = _valid_setting_value(step.get("setting"),
                                                 value)
                elif field_type is dict:
                    valid = isinstance(value, dict) and bool(value)
                elif field_type is list:
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
                    # Include the offending value: the feedback retry
                    # shows it to the model, which usually corrects a
                    # mistyped field on the second attempt.
                    raise PlannerValidationError(
                        f"planner field {field} has an invalid type or "
                        f"value (got {repr(value)[:80]})")