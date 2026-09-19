from tools.desktop_control import DesktopController
from tools.computer import ComputerController
from tools.vision import ScreenVision
from tools.whatsapp import WhatsAppManager
from tools.filesystem import FilesystemTools
from tools.terminal import TerminalTool
from tools.git_tool import GitTool
from tools.python_dev import PythonDevTools
import time
import logging
from backend.interfaces import ToolResult


class TaskExecutor:

    def __init__(self):
        self.desktop = DesktopController()
        self.computer = ComputerController()
        self.vision = ScreenVision()
        self.whatsapp = WhatsAppManager(desktop=self.desktop, computer=self.computer, vision=self.vision)
        self.fs = FilesystemTools()
        self.terminal = TerminalTool()
        self.git = GitTool()
        self.pydev = PythonDevTools()

    def execute(self, plan: dict):
        goal = plan.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return ToolResult("plan", False, "Invalid task goal", {"started": False, "completed": False})

        last_result = None
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
            last_result = result
            time.sleep(0.1)

        # Return the FINAL step's result so its verification data (stdout,
        # evidence, verified) and tool-specific message survive to the caller.
        # Collapsing to a generic "Task completed" used to erase all of it.
        if last_result is not None:
            return last_result
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

            # ---------------- Messaging ----------------
            elif tool == "send_whatsapp":
                recipient = step.get("recipient") or ""
                message = step.get("message") or ""
                if not recipient.strip() or not message.strip():
                    return ToolResult(
                        tool,
                        False,
                        "WhatsApp needs both a recipient and a message",
                        {"started": False, "completed": False, "verified": False},
                    )
                return self.whatsapp.send_message(recipient, message)

            # ---------------- Filesystem ----------------
            elif tool == "list_files":
                return self.fs.list_files(step.get("directory", "."))

            elif tool == "inspect_file":
                return self.fs.inspect_file(step["path"])

            elif tool == "create_file":
                return self.fs.create_file(step["path"], step.get("content", ""))

            elif tool == "edit_file":
                return self.fs.edit_file(step["path"], step["content"])

            elif tool == "move_file":
                return self.fs.move_file(step["src"], step["dst"])

            elif tool == "search_files":
                return self.fs.search_files(step["directory"], step["pattern"])

            elif tool == "delete_file":
                return self.fs.delete_file(step["path"])

            # ---------------- Terminal ----------------
            elif tool == "execute_terminal":
                return self.terminal.execute_terminal(step["command"], timeout=step.get("timeout", 30.0), cwd=step.get("cwd"))

            # ---------------- Git ----------------
            elif tool == "git_status":
                return self.git.git_status()

            elif tool == "git_diff":
                return self.git.git_diff()

            elif tool == "git_log":
                return self.git.git_log(n=step.get("n", 5))

            elif tool == "git_branch":
                return self.git.git_branch()

            # ---------------- Python Dev ----------------
            elif tool == "run_python_script":
                return self.pydev.run_python_script(step["script_path"], args=step.get("args"))

            elif tool == "run_pytest":
                return self.pydev.run_pytest(test_path=step.get("test_path"))

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