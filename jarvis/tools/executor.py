from tools.desktop_control import DesktopController
from tools.computer import ComputerController
from tools.vision import ScreenVision
from tools.visual import VisualAgent
from tools.whatsapp import WhatsAppManager
from tools.filesystem import FilesystemTools
from tools.organizer import FileOrganizer
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
        self.visual = VisualAgent(computer=self.computer, vision=self.vision)
        self.whatsapp = WhatsAppManager(desktop=self.desktop, computer=self.computer, vision=self.vision)
        self.fs = FilesystemTools()
        self.organizer = FileOrganizer()
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
                # `preferred` is the exact chat the user picked when the name
                # matched more than one conversation.
                preferred = str(step.get("preferred") or "").strip() or None
                return self.whatsapp.send_message(recipient, message, preferred=preferred)

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

            # ---------------- File organization ----------------
            elif tool == "analyze_directory":
                return self.organizer.analyze_directory(
                    step.get("directory", "."), bool(step.get("recursive", False))
                )

            elif tool == "find_duplicates":
                return self.organizer.find_duplicates(
                    step.get("directory", "."), bool(step.get("recursive", False))
                )

            elif tool == "organize_directory":
                return self.organizer.organize_directory(
                    step["directory"],
                    strategy=step.get("strategy", "by_type"),
                    dry_run=bool(step.get("dry_run", True)),
                    older_than_days=step.get("older_than_days"),
                    recursive=bool(step.get("recursive", False)),
                )

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

            # ---------------- Visual computer agent (Milestone 3) ----------------
            # Observe/understand/locate/act/verify through the shared
            # VisualAgent. Results are ToolResults with serializable
            # evidence — in-memory PIL images never leave this layer.

            elif tool == "screenshot":
                shot = self.visual.capture(
                    mode=step.get("mode", "active"),
                    region=step.get("region"),
                )
                if not shot.get("ok"):
                    return ToolResult(
                        tool, False,
                        shot.get("error") or "screenshot failed",
                        {"started": True, "completed": False,
                         "reason": shot.get("reason", "capture_unavailable")},
                    )
                return ToolResult(tool, True, "screenshot captured", {
                    "started": True, "completed": True,
                    "mode": shot["mode"], "width": shot["width"],
                    "height": shot["height"], "origin": shot["origin"],
                    "screenshot_ms": shot["screenshot_ms"],
                })

            elif tool == "inspect_screen":
                model = self.visual.inspect(mode=step.get("mode", "active"))
                if not model.get("ok"):
                    return ToolResult(
                        tool, False,
                        model.get("error") or "screen inspection failed",
                        {"started": True, "completed": False,
                         "reason": model.get("reason", "observe_failed")},
                    )
                return ToolResult(tool, True,
                    f"screen inspected: {model['element_count']} elements"
                    + (" (uncertain)" if model["uncertain"] else ""), {
                    "started": True, "completed": True,
                    "window": model["window"], "app": model["app"],
                    "element_count": model["element_count"],
                    "elements": model["elements"][:50],
                    "text": model["text"][:2000],
                    "uncertain": model["uncertain"],
                    "timings": model["timings"],
                })

            elif tool == "locate_target":
                found = self.visual.locate(
                    step["target"],
                    min_confidence=step.get("min_confidence", 0.6),
                )
                if not found.get("ok"):
                    return ToolResult(
                        tool, False, found.get("reason") or "locate failed",
                        {"started": True, "completed": False,
                         "reason": found.get("reason", "locate_failed")},
                    )
                if not found.get("found"):
                    return ToolResult(
                        tool, False,
                        f"target not confidently found: {step['target']}",
                        {"started": True, "completed": False,
                         "reason": found.get("reason", "low_confidence"),
                         "candidates": found.get("candidates", [])},
                    )
                return ToolResult(tool, True,
                    f"target located: {found['located']['label']}", {
                    "started": True, "completed": True,
                    "target": found["located"],
                    "candidates": found.get("candidates", []),
                    "locate_ms": found.get("locate_ms"),
                })

            elif tool == "visual_click":
                res = self.visual.visual_click(
                    step["target"],
                    button=step.get("button", "left"),
                    min_confidence=step.get("min_confidence", 0.6),
                    expected_window=step.get("expected_window"),
                    verify_text=step.get("verify_text"),
                )
                return self._visual_outcome(tool, res, "clicked")

            elif tool == "visual_type":
                res = self.visual.visual_type(
                    step["text"],
                    target=step.get("target"),
                    min_confidence=step.get("min_confidence", 0.6),
                    expected_window=step.get("expected_window"),
                    verify_text=step.get("verify_text"),
                )
                return self._visual_outcome(tool, res, "typed")

            elif tool == "visual_drag":
                res = self.visual.visual_drag(
                    step["target"],
                    step["to_target"],
                    min_confidence=step.get("min_confidence", 0.6),
                )
                return self._visual_outcome(tool, res, "dragged")

            elif tool == "visual_scroll":
                res = self.visual.scroll_at(
                    step["amount"], point=step.get("point"),
                )
                if not res.get("ok"):
                    return ToolResult(
                        tool, False, res.get("reason") or "scroll failed",
                        {"started": True, "completed": False,
                         "reason": res.get("reason", "action_failed")},
                    )
                return ToolResult(tool, True, "scrolled", {
                    "started": True, "completed": True,
                    "amount": res["amount"], "action_ms": res["action_ms"],
                })

            elif tool == "visual_verify":
                res = self.visual.verify({
                    "kind": step["kind"],
                    "text": step.get("text"),
                    "title": step.get("title"),
                })
                if not res.get("ok"):
                    return ToolResult(
                        tool, False, res.get("reason") or "verify failed",
                        {"started": True, "completed": False,
                         "reason": res.get("reason", "invalid_expectation")},
                    )
                return ToolResult(tool, True,
                    "visual state verified" if res["verified"]
                    else "visual state not observed", {
                    "started": True, "completed": True,
                    "verified": res["verified"], "kind": res["kind"],
                    "evidence": res["evidence"],
                    "verify_ms": res["verify_ms"],
                })

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
    def _visual_outcome(tool, res, verb):
        """ToolResult from a VisualAgent guarded op (never bare claims)."""
        if not res.get("ok"):
            return ToolResult(
                tool, False, res.get("reason") or f"visual {verb} failed",
                {"started": True, "completed": False,
                 "reason": res.get("reason", "action_failed"),
                 "verified": False,
                 "evidence": res.get("evidence", {}),
                 "retries": res.get("retries", 0)},
            )
        return ToolResult(
            tool, True,
            f"visual {verb} verified" if res.get("verified")
            else f"visual {verb} completed without verification", {
            "started": True, "completed": True,
            "verified": bool(res.get("verified")),
            "target": res.get("located") or res.get("from"),
            "evidence": res.get("evidence", {}),
            "retries": res.get("retries", 0),
            "total_ms": res.get("total_ms")},
        )

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
            "screenshot": ("mode", str),
            "inspect_screen": ("mode", str),
            "locate_target": ("target", str),
            "visual_click": ("target", str),
            "visual_type": ("text", str),
            "visual_drag": ("target", str),
            "visual_scroll": ("amount", int),
            "visual_verify": ("kind", str),
        }
        if tool in ("screenshot", "inspect_screen"):
            mode = step.get("mode", "active")
            if mode not in ("active", "full", "region"):
                return ToolResult(tool, False, "invalid mode", {"started": False, "completed": False})
            if mode == "region" and not isinstance(step.get("region"), dict):
                return ToolResult(tool, False, "invalid region", {"started": False, "completed": False})
            return None
        if tool in ("locate_target", "visual_click", "visual_type", "visual_drag"):
            conf = step.get("min_confidence", 0.6)
            if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
                return ToolResult(tool, False, "invalid min_confidence", {"started": False, "completed": False})
            target = step.get("target")
            # visual_type may type into the already-focused field, so its
            # target is optional; every other visual tool must name one.
            if tool == "visual_type" and target is None:
                pass
            elif not isinstance(target, str) or not target.strip():
                return ToolResult(tool, False, "invalid target", {"started": False, "completed": False})
            if tool == "visual_click" and step.get("button", "left") not in ("left", "right", "double"):
                return ToolResult(tool, False, "invalid button", {"started": False, "completed": False})
            if tool == "visual_drag":
                dest = step.get("to_target")
                if not isinstance(dest, str) or not dest.strip():
                    return ToolResult(tool, False, "invalid to_target", {"started": False, "completed": False})
            if tool == "visual_type":
                text = step.get("text")
                if not isinstance(text, str) or not text or len(text) > 2000:
                    return ToolResult(tool, False, "invalid text", {"started": False, "completed": False})
            return None
        if tool == "visual_verify":
            if step.get("kind") not in ("text_visible", "text_absent", "window_active", "window_closed"):
                return ToolResult(tool, False, "invalid kind", {"started": False, "completed": False})
            return None
        if tool == "visual_scroll":
            amount = step.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, int):
                return ToolResult(tool, False, "invalid amount", {"started": False, "completed": False})
            return None
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