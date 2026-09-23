from tools.desktop_control import DesktopController
from tools.computer import ComputerController
from tools.vision import ScreenVision
from tools.visual import VisualAgent
from tools.windows import WindowManager
from tools.workflows import WorkflowEngine
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
        self.windows = WindowManager(computer=self.computer,
                                     desktop=self.desktop)
        self.workflows = WorkflowEngine(
            step_runner=lambda step: self._execute_step(step["tool"], step),
            visual=self.visual, windows=self.windows,
            computer=self.computer)
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
                app = step["app"]
                if not self.desktop.open_app(app):
                    return self._outcome(
                        tool, False,
                        f"I couldn't find an app called '{app}'.")
                if self.computer.wait_for_window(app, timeout=10):
                    return ToolResult(
                        tool, True, f"{app} is now open.",
                        {"started": True, "completed": True,
                         "verified": True})
                return ToolResult(
                    tool, True,
                    f"{app} is open, but I couldn't bring its window "
                    f"forward.",
                    {"started": True, "completed": True,
                     "verified": False})

            elif tool == "wait_window":
                title = step["title"]
                found = self.computer.wait_for_window(title)
                if found:
                    return self._outcome(tool, self.computer.focus_window(title), f"I couldn't focus '{title}'.")
                return self._outcome(tool, False, f"'{title}' did not appear.")

            elif tool == "close_app":
                app = step["app"]
                if self.desktop.close_app(app):
                    return ToolResult(
                        tool, True, f"Closed {app}.",
                        {"started": True, "completed": True,
                         "verified": True})
                return self._outcome(
                    tool, False,
                    f"I couldn't find an open app called '{app}'.")

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
                    return self._target_failure_result(
                        tool, step["target"], {
                            **found,
                            "reason": found.get("reason", "low_confidence")},
                        f"target not confidently found: {step['target']}")
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

            # ---------------- Windows + workflows (Milestone 4) ----------------
            # Cross-application automation through the shared
            # WindowManager / WorkflowEngine. Results carry serializable
            # evidence; window handles never leave this layer as live
            # objects (hwnd ints only).

            elif tool == "list_windows":
                res = self.windows.list_windows(
                    pattern=step.get("pattern"), limit=30)
                if not res.get("ok"):
                    return ToolResult(
                        tool, False, res.get("error") or "list failed",
                        {"started": True, "completed": False,
                         "reason": res.get("reason", "enumerate_failed")},
                    )
                return ToolResult(tool, True,
                    f"{res['count']} windows listed", {
                    "started": True, "completed": True,
                    "windows": res["windows"], "count": res["count"],
                    "list_ms": res["list_ms"],
                })

            elif tool == "switch_app":
                res = self.windows.focus(step["target"])
                if not res.get("ok"):
                    return self._target_failure_result(
                        tool, step["target"], res, "switch failed")
                window = res["window"]
                title = window.get("title") or step.get("target", "window")
                selection = res.get("selection")
                picked = (f" (using the {selection.replace('_', ' ')})"
                          if selection else "")
                return ToolResult(tool, True,
                    f"Switched to {title}{picked}.", {
                    "started": True, "completed": True, "verified": True,
                    # Focus is verified by hwnd (titles can repeat).
                    "target": {"label": title, "confidence": 0.95},
                    "app": window.get("app"),
                    "focus_ms": res.get("focus_ms")},
                )

            elif tool == "window_manage":
                action = step.get("action")
                if action in ("minimize", "maximize", "restore"):
                    res = self.windows.set_state(step["target"], action)
                elif action == "move_resize":
                    res = self.windows.move_resize(
                        step["target"], step.get("x"), step.get("y"),
                        step.get("width"), step.get("height"))
                elif action in ("snap_left", "snap_right",
                                    "snap_top", "snap_bottom"):
                    res = self.windows.snap(
                        step["target"],
                        {"snap_left": "left", "snap_right": "right",
                         "snap_top": "top",
                         "snap_bottom": "bottom"}[action])
                elif action == "health":
                    res = self.windows.health(step["target"])
                    if res.get("ok"):
                        return ToolResult(tool, True,
                            "window responsive"
                            if res.get("responsive")
                            else "window unresponsive", {
                            "started": True, "completed": True,
                            "verified": True,
                            "responsive": res.get("responsive"),
                            "health_ms": res.get("health_ms")},
                        )
                elif action == "close":
                    res = self.windows.close_window(step["target"])
                elif action == "restart":
                    res = self.windows.restart_app(
                        step["target"], app=step.get("app"))
                else:
                    return ToolResult(
                        tool, False, "invalid action",
                        {"started": False, "completed": False})
                if not res.get("ok"):
                    return self._target_failure_result(
                        tool, step.get("target", "window"),
                        res, "manage failed")
                title = (res.get("window") or {}).get("title") \
                    or step.get("target", "window")
                spoken = {
                    "minimize": f"{title} is now minimized.",
                    "maximize": f"{title} is now maximized.",
                    "restore": f"{title} is restored.",
                    "move_resize": f"{title} is moved.",
                    "snap_left": f"{title} is on the left.",
                    "snap_right": f"{title} is on the right.",
                    "snap_top": f"{title} is on the top half.",
                    "snap_bottom": f"{title} is on the bottom half.",
                    "close": f"Closed {title}.",
                    "restart": f"{title} is restarted.",
                }.get(action, f"window {action} verified")
                return ToolResult(tool, True, spoken, {
                    "started": True, "completed": True, "verified": True,
                    "action": action, "window": res.get("window")},
                )

            elif tool == "extract_window_text":
                res = self._extract_text(step)
                if not res.get("ok"):
                    return ToolResult(
                        tool, False, res.get("reason") or "extract failed",
                        {"started": True, "completed": False,
                         "reason": res.get("reason", "extract_failed"),
                         "verified": False},
                    )
                return ToolResult(tool, True,
                    f"extracted {res.get('chars', 0)} chars "
                    f"via {res.get('method')}", {
                    "started": True, "completed": True, "verified": True,
                    "text": res.get("text", ""),
                    "method": res.get("method"),
                    "window": res.get("window")},
                )

            elif tool == "read_clipboard":
                try:
                    text = str(self.computer.get_clipboard() or "")
                except Exception as exc:
                    return ToolResult(
                        tool, False, "clipboard unreadable",
                        {"started": True, "completed": False,
                         "reason": "clipboard_failed",
                         "error": str(exc)[:200]},
                    )
                return ToolResult(tool, True,
                    f"clipboard read ({len(text)} chars)", {
                    "started": True, "completed": True, "verified": True,
                    "text": text[:2000], "length": len(text),
                    "truncated": len(text) > 2000},
                )

            elif tool == "run_workflow":
                result = self.workflows.run_workflow(
                    step.get("goal", ""), step.get("steps", []),
                    max_retries=step.get("max_retries", 1))
                return result

            elif tool == "visual_menu":
                action = step.get("action", "open")
                if action not in ("open", "close"):
                    return ToolResult(
                        tool, False, "invalid action",
                        {"started": False, "completed": False})
                res = self.visual.visual_menu(
                    step["target"],
                    action=action,
                    min_confidence=step.get("min_confidence", 0.6),
                )
                if not res.get("ok"):
                    reason = res.get("reason") or "action_failed"
                    if reason == "menu_not_observed":
                        message = (f"I clicked '{step['target']}' but "
                                   f"no menu appeared.")
                    elif reason == "menu_still_visible":
                        message = (f"The menu stayed open after trying "
                                   f"several ways to close it.")
                    else:
                        message = self._human_target_failure(
                            step["target"], res, f"menu {action} failed")
                    return ToolResult(
                        tool, False, message, {
                        "started": True, "completed": False,
                        "reason": reason,
                        "verified": False,
                        "evidence": res.get("evidence", {})},
                    )
                acted = "opened" if action == "open" else "closed"
                return ToolResult(tool, True,
                    f"menu {acted} and verified", {
                    "started": True, "completed": True, "verified": True,
                    "target": res.get("located"),
                    "evidence": res.get("evidence", {}),
                    "retries": res.get("retries", 0)},
                )

            # ---------------- Semantic Vision ----------------

            elif tool == "click_text":
                try:
                    success = self.vision.click_text(step["text"])
                except Exception as exc:
                    reason = getattr(self.vision, "last_error", None) or "click failure"
                    raise RuntimeError(reason) from exc
                if success:
                    return ToolResult(
                        tool, True, f"Clicked '{step['text']}'.",
                        {"started": True, "completed": True,
                         "verified": False})
                reason = getattr(self.vision, "last_error", None) or "target not found"
                if reason == "OCR failure":
                    return ToolResult(
                        tool, False, "I couldn't read the screen clearly.",
                        {"started": True, "completed": False,
                         "reason": reason, "verified": False})
                return self._target_failure_result(
                    tool, step["text"],
                    {"reason": "not_found"
                     if reason == "target not found" else reason},
                    reason)

            else:
                return ToolResult(tool, False, f"Unknown tool: {tool}", {"started": False, "completed": False})

    def _extract_text(self, step):
        """Hybrid extraction: clipboard when asked, OCR otherwise.

        ``method`` is ``ocr`` (read-only, default), ``clipboard``
        (select-all + copy; exact text but disturbs selection) or
        ``auto`` (clipboard first with OCR fallback). The clipboard is
        preserved across clipboard-method reads.
        """
        method = step.get("method", "ocr")
        target = step.get("target")
        window = None
        if target:
            resolved = self.windows.resolve(target)
            if resolved.get("ok") and resolved.get("found"):
                window = resolved["window"].get("title")
                focused = self.windows.focus(target)
                if not focused.get("ok"):
                    return {"ok": False,
                            "reason": focused.get("reason", "focus_failed")}
                window = focused["window"].get("title")
            else:
                return {"ok": False,
                        "reason": resolved.get("reason", "target not found")}
        else:
            active = self.windows.active()
            window = active.get("title") if active.get("available") else None

        def _ocr():
            model = self.visual.inspect()
            if not model.get("ok"):
                return {"ok": False, "reason": "observe_failed"}
            return {"ok": True, "text": model.get("text", "")[:2000],
                    "method": "ocr", "window": window}

        if method == "ocr":
            return _ocr()
        if method in ("clipboard", "auto"):
            saved = None
            try:
                saved = self.computer.get_clipboard()
            except Exception:
                saved = None
            try:
                self.computer.hotkey("ctrl", "a")
                self.computer.hotkey("ctrl", "c")
                text = str(self.computer.get_clipboard() or "")
            except Exception as exc:
                return {"ok": False, "reason": "clipboard_failed",
                        "error": str(exc)[:200]}
            finally:
                try:
                    if saved is not None:
                        self.computer.set_clipboard(saved)
                except Exception:
                    pass
            if text.strip():
                return {"ok": True, "text": text[:2000],
                        "method": "clipboard", "window": window,
                        "chars": len(text)}
            if method == "clipboard":
                return {"ok": False, "reason": "clipboard_empty"}
            return _ocr()
        return {"ok": False, "reason": "invalid_method"}

    @staticmethod
    def _human_target_failure(target, res, default):
        """User-facing sentence for target-resolution failures.

        Codes (ambiguous / low_confidence / not_found) stay in
        ``data["reason"]`` for the planner; the spoken message names
        what was seen and, for ambiguity, asks which one to use.
        """
        reason = res.get("reason") or "target not found"
        candidates = res.get("candidates", []) or []
        names = [c.get("label", c.get("title", "?")) for c in candidates]
        if reason == "ambiguous" and names:
            shown = ", ".join(f"'{n}'" for n in names[:3])
            return (f"I found {len(names)} matches for '{target}' "
                    f"({shown}). Which one should I use?")
        if reason == "low_confidence":
            return f"I couldn't confidently find '{target}'."
        if reason in ("not_found", "target not found"):
            return f"I couldn't find '{target}'."
        if reason == "focus_failed":
            return f"I couldn't bring '{target}' forward."
        if reason in ("snap_not_reached", "move_not_reached",
                      "state_not_reached"):
            title = (res.get("window") or {}).get("title") or target
            return (f"'{title}' didn't reach the requested state. "
                    f"It may be busy or still animating.")
        return res.get("message") or default

    def _target_failure_result(self, tool, target, res, default):
        return ToolResult(
            tool, False,
            self._human_target_failure(target, res, default),
            {"started": True, "completed": False,
             "reason": res.get("reason", "target not found"),
             "verified": False,
             "candidates": res.get("candidates", [])},
        )

    def _visual_outcome(self, tool, res, verb):
        """ToolResult from a VisualAgent guarded op (never bare claims)."""
        if not res.get("ok"):
            target = res.get("target")
            if isinstance(target, dict):
                target = target.get("label", "")
            message = self._human_target_failure(
                target or "the target", res, f"visual {verb} failed")
            return ToolResult(
                tool, False, message,
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
            "visual_menu": ("target", str),
            "list_windows": ("pattern", str),
            "switch_app": ("target", str),
            "window_manage": ("target", str),
            "extract_window_text": ("target", str),
            "read_clipboard": ("__none__", str),
            "run_workflow": ("goal", str),
        }
        if tool == "list_windows":
            pattern = step.get("pattern")
            if pattern is not None and (
                    not isinstance(pattern, str) or not pattern.strip()):
                return ToolResult(tool, False, "invalid pattern", {"started": False, "completed": False})
            return None
        if tool == "switch_app":
            target = step.get("target")
            if not isinstance(target, str) or not target.strip():
                return ToolResult(tool, False, "invalid target", {"started": False, "completed": False})
            return None
        if tool == "window_manage":
            target = step.get("target")
            if not isinstance(target, str) or not target.strip():
                return ToolResult(tool, False, "invalid target", {"started": False, "completed": False})
            action = step.get("action")
            if action not in ("minimize", "maximize", "restore",
                              "move_resize", "snap_left", "snap_right",
                              "snap_top", "snap_bottom",
                              "health", "close", "restart"):
                return ToolResult(tool, False, "invalid action", {"started": False, "completed": False})
            if action == "move_resize":
                for field in ("x", "y", "width", "height"):
                    value = step.get(field)
                    if isinstance(value, bool) or not isinstance(value, int):
                        return ToolResult(tool, False, f"invalid {field}", {"started": False, "completed": False})
            return None
        if tool == "extract_window_text":
            target = step.get("target")
            if target is not None and (
                    not isinstance(target, str) or not target.strip()):
                return ToolResult(tool, False, "invalid target", {"started": False, "completed": False})
            if step.get("method", "ocr") not in ("ocr", "clipboard", "auto"):
                return ToolResult(tool, False, "invalid method", {"started": False, "completed": False})
            return None
        if tool == "read_clipboard":
            return None
        if tool == "run_workflow":
            goal = step.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                return ToolResult(tool, False, "invalid goal", {"started": False, "completed": False})
            sub = step.get("steps")
            if not isinstance(sub, list) or not sub or len(sub) > 50:
                return ToolResult(tool, False, "invalid steps", {"started": False, "completed": False})
            for s in sub:
                if not isinstance(s, dict) or not isinstance(s.get("tool"), str):
                    return ToolResult(tool, False, "invalid nested step", {"started": False, "completed": False})
            return None
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
        if tool == "visual_menu":
            target = step.get("target")
            if not isinstance(target, str) or not target.strip():
                return ToolResult(tool, False, "invalid target", {"started": False, "completed": False})
            if step.get("action", "open") not in ("open", "close"):
                return ToolResult(tool, False, "invalid action", {"started": False, "completed": False})
            conf = step.get("min_confidence", 0.6)
            if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
                return ToolResult(tool, False, "invalid min_confidence", {"started": False, "completed": False})
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