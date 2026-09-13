from tools.desktop_control import DesktopController
from tools.computer import ComputerController
from tools.vision import ScreenVision
import time
import logging
from backend.interfaces import ToolResult


class TaskExecutor:

    def __init__(self):
        self.desktop = DesktopController()
        self.computer = ComputerController()
        self.vision = ScreenVision()

    def execute(self, plan: dict):
        goal = plan.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return ToolResult("plan", False, "Invalid task goal", {"started": False, "completed": False})

        for i, step in enumerate(plan["steps"], start=1):

            tool = step["tool"]
            try:
                result = self._execute_step(tool, step)
            except Exception as exc:
                logging.getLogger("jarvis.tools").exception("tool %s failed", tool)
                if str(exc).startswith("OCR failure:"):
                    reason = "OCR failure"
                elif str(exc).startswith("click failure:"):
                    reason = "click failure"
                else:
                    reason = "controller failure"
                return ToolResult(
                    tool,
                    False,
                    f"{tool} failed",
                    {"reason": reason, "started": True, "completed": False},
                )
            if not result.success:
                return result
            time.sleep(0.1)
        return ToolResult("task", True, "Task completed", {"started": True, "completed": True})

    def _execute_step(self, tool, step):
            validation = self._validate_step(tool, step)
            if validation is not None:
                return validation
            # ---------------- Desktop ----------------
            if tool == "open_app":
                ok = self.desktop.open_app(step["app"])
                return self._outcome(tool, ok, "app not found or launch failed")

            elif tool == "wait_window":
                found = self.computer.wait_for_window(step["title"])
                if found:
                    return self._outcome(tool, self.computer.focus_window(step["title"]), "window could not be focused")
                return self._outcome(tool, False, "window not found before timeout")

            elif tool == "close_app":
                return self._outcome(tool, self.desktop.close_app(step["app"]), "application not found or close failed")

            # ---------------- Browser ----------------

            elif tool == "open_youtube":
                return self._outcome(tool, self.desktop.open_youtube(), "browser open failed")

            elif tool == "search_youtube":
                return self._outcome(tool, self.desktop.search_youtube(step["query"]), "browser navigation failed")

            elif tool == "search_google":
                return self._outcome(tool, self.desktop.search_google(step["query"]), "browser navigation failed")

            # ---------------- Keyboard ----------------

            elif tool == "type":
                self.computer.type_text(step["text"])
                return ToolResult(tool, True, "text typed", {"started": True, "completed": True})

            elif tool == "press":
                self.computer.press(step["key"])
                return ToolResult(tool, True, "key pressed", {"started": True, "completed": True})

            elif tool == "hotkey":
                self.computer.hotkey(*step["keys"])
                return ToolResult(tool, True, "hotkey pressed", {"started": True, "completed": True})

            # ---------------- Semantic Vision ----------------

            elif tool == "click_text":
                try:
                    success = self.vision.click_text(step["text"])
                except Exception as exc:
                    reason = getattr(self.vision, "last_error", None) or "click failure"
                    raise RuntimeError(reason) from exc
                reason = getattr(self.vision, "last_error", None) or "target not found"
                return self._outcome(tool, success, reason)

            else:
                return ToolResult(tool, False, f"Unknown tool: {tool}", {"started": False, "completed": False})

    @staticmethod
    def _outcome(tool, success, reason):
        return ToolResult(
            tool,
            bool(success),
            f"{tool} completed" if success else reason,
            {"started": True, "completed": bool(success), **({} if success else {"reason": reason})},
        )

    @staticmethod
    def _validate_step(tool, step):
        required = {
            "open_app": ("app", str),
            "wait_window": ("title", str),
            "close_app": ("app", str),
            "search_youtube": ("query", str),
            "search_google": ("query", str),
            "type": ("text", str),
            "press": ("key", str),
            "click_text": ("text", str),
        }
        if tool == "hotkey":
            keys = step.get("keys")
            if not isinstance(keys, list) or not keys or not all(isinstance(key, str) and key.strip() for key in keys):
                return ToolResult(tool, False, "invalid hotkey sequence", {"started": False, "completed": False})
            return None
        field = required.get(tool)
        if field is None:
            return None
        name, expected = field
        value = step.get(name)
        if not isinstance(value, expected) or not value.strip():
            return ToolResult(tool, False, f"invalid {name}", {"started": False, "completed": False})
        if tool == "press" and value.lower() not in {
            "enter", "escape", "esc", "tab", "space", "backspace", "delete",
            "up", "down", "left", "right", "home", "end", "ctrl", "shift",
            "alt", "win",
        } and len(value) != 1:
            return ToolResult(tool, False, "invalid key", {"started": False, "completed": False})
        return None