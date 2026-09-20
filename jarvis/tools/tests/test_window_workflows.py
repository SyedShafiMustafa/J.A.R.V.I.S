"""Milestone 4 — active window + cross-app automation tests.

All fakes: no real windows, hotkeys, clipboard or OCR are touched.
Real-machine validation lives in ``scripts/test_window_validation.py``.
"""

import sys
import types

import pytest

from backend.interfaces import ToolResult
from tools.windows import WindowManager
from tools.workflows import WorkflowEngine

# ---------------------------------------------------------------------------
# Fake desktop: windows, foreground, win32
# ---------------------------------------------------------------------------


class FakeWin:
    def __init__(self, state, hwnd, title, rect=(0, 0, 800, 600)):
        self._state = state
        self._hWnd = hwnd
        self.title = title
        self.left, self.top, self.width, self.height = (
            rect[0], rect[1], rect[2], rect[3])
        self._min = False
        self._max = False

    @property
    def isMinimized(self):
        return self._min

    @property
    def isMaximized(self):
        return self._max

    @property
    def isActive(self):
        return self._state.get("active") == self._hWnd

    def minimize(self):
        self._min = True
        self._max = False

    def maximize(self):
        self._max = True
        self._min = False

    def restore(self):
        self._min = False
        self._max = False

    def moveTo(self, x, y):
        self.left, self.top = x, y

    def resizeTo(self, w, h):
        self.width, self.height = w, h


class FakeGW:
    state = {"windows": [], "active": None}

    @classmethod
    def reset(cls, windows, active=None):
        cls.state = {"windows": list(windows), "active": active}

    @classmethod
    def getAllWindows(cls):
        return list(cls.state["windows"])

    @classmethod
    def getActiveWindow(cls):
        for w in cls.state["windows"]:
            if w._hWnd == cls.state["active"]:
                return w
        return None

    @classmethod
    def getWindowsWithTitle(cls, title):
        needle = (title or "").lower()
        return [w for w in cls.state["windows"]
                if needle in (w.title or "").lower()]


def install_fakes(monkeypatch, hung_hwnds=(), fail_fg=()):
    from tools import windows as windows_mod
    monkeypatch.setattr(windows_mod, "gw", FakeGW)

    fake_w32 = types.ModuleType("win32gui")
    fake_w32.IsWindow = lambda hwnd: any(
        w._hWnd == hwnd for w in FakeGW.state["windows"])

    def _set_fg(hwnd):
        if hwnd in fail_fg:
            raise RuntimeError("denied")
        if fake_w32.IsWindow(hwnd):
            FakeGW.state["active"] = hwnd
        else:
            raise RuntimeError("no such window")

    fake_w32.SetForegroundWindow = _set_fg

    def _smt(hwnd, msg, w, l, flags, timeout):
        # Real SendMessageTimeout returns (return-code, message-result):
        # nonzero code = the thread answered in time.
        return (0, 0) if hwnd in hung_hwnds else (1, 1)

    fake_w32.SendMessageTimeout = _smt

    def _post(hwnd, msg, w, l):
        if msg == 16:  # WM_CLOSE
            FakeGW.state["windows"] = [
                x for x in FakeGW.state["windows"] if x._hWnd != hwnd]
            if FakeGW.state["active"] == hwnd:
                FakeGW.state["active"] = None

    fake_w32.PostMessage = _post
    monkeypatch.setitem(sys.modules, "win32gui", fake_w32)

    fake_con = types.ModuleType("win32con")
    fake_con.WM_NULL = 0
    fake_con.SMTO_ABORTIFHUNG = 2
    fake_con.WM_CLOSE = 16
    monkeypatch.setitem(sys.modules, "win32con", fake_con)
    return fake_w32


class FakeComputer:
    def __init__(self):
        self.calls = []
        self.clipboard = ""

    def screen_size(self):
        return {"width": 1920, "height": 1080}

    def focus_window(self, title):
        self.calls.append(("focus", title))
        matches = FakeGW.getWindowsWithTitle(title)
        if not matches:
            return False
        FakeGW.state["active"] = matches[0]._hWnd
        return True

    def wait_for_window(self, title, timeout=10):
        return bool(FakeGW.getWindowsWithTitle(title))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", keys))
        # Simulate native snap on the focused fake window.
        if keys[:1] == ("win",) and keys[1:] in (("left",), ("right",)):
            active = FakeGW.getActiveWindow()
            if active is not None:
                half = 960
                if keys[1] == "left":
                    active.moveTo(0, 0)
                else:
                    active.moveTo(half, 0)
                active.resizeTo(half, 1080)

    def press(self, key):
        self.calls.append(("press", key))

    def get_clipboard(self):
        return self.clipboard

    def set_clipboard(self, text):
        self.clipboard = text


class FakeDesktop:
    def __init__(self):
        self.opened = []

    def open_app(self, app):
        self.opened.append(app)
        return True


class FakeVisual:
    def __init__(self):
        self.text = ""
        self.verify_result = {"verified": True, "evidence": {}}

    def inspect(self, **kwargs):
        return {"ok": True, "text": self.text}

    def verify(self, expectation, timeout=10.0):
        return {"ok": True,
                "verified": bool(self.verify_result["verified"]),
                "kind": expectation.get("kind"),
                "evidence": dict(self.verify_result["evidence"])}


def make_windows(monkeypatch, wins, active=None, **kw):
    state = {"windows": [], "active": active}
    FakeGW.state = state
    objs = []
    for i, spec in enumerate(wins):
        title = spec if isinstance(spec, str) else spec[0]
        rect = (0, 0, 800, 600) if isinstance(spec, str) else spec[1]
        w = FakeWin(FakeGW.state, 100 + i, title, rect)
        objs.append(w)
    FakeGW.state["windows"] = objs
    if active == "first" and objs:
        FakeGW.state["active"] = objs[0]._hWnd
    install_fakes(monkeypatch, **kw)
    computer = FakeComputer()
    return WindowManager(computer=computer, desktop=FakeDesktop())


# ---------------------------------------------------------------------------
# UNIT — list / identify / targeting
# ---------------------------------------------------------------------------

def test_list_windows_schema_and_pattern(monkeypatch):
    mgr = make_windows(monkeypatch, ["Untitled - Notepad", "New Tab - Chrome",
                                     ""])
    res = mgr.list_windows()
    assert res["ok"] is True and res["count"] == 2
    entry = res["windows"][0]
    assert set(entry) >= {"hwnd", "title", "app", "visible", "minimized",
                          "maximized", "active", "rect"}
    assert entry["app"] == "Notepad"
    filtered = mgr.list_windows(pattern="chrome")
    assert filtered["count"] == 1
    assert filtered["windows"][0]["app"] == "Chrome"


def test_list_filters_dead_hwnds(monkeypatch):
    mgr = make_windows(monkeypatch, ["Alive - Notepad"])
    ghost = FakeWin(FakeGW.state, 9999, "Ghost - Notepad")
    FakeGW.state["windows"].append(ghost)

    real_w32 = sys.modules.get("win32gui")
    import tools.windows as windows_mod
    orig = windows_mod._is_alive
    monkeypatch.setattr(windows_mod, "_is_alive",
                        lambda hwnd: hwnd != 9999)
    try:
        res = mgr.list_windows()
    finally:
        monkeypatch.setattr(windows_mod, "_is_alive", orig)
    assert [w["title"] for w in res["windows"]] == ["Alive - Notepad"]
    assert real_w32 is not None  # sanity: fakes installed over real one


def test_list_retries_empty_title_race(monkeypatch):
    mgr = make_windows(monkeypatch, ["Untitled - Notepad"])
    calls = {"n": 0}
    real_all = FakeGW.getAllWindows

    @classmethod
    def _flaky(cls):
        calls["n"] += 1
        if calls["n"] == 1:
            # First snapshot: title not populated yet.
            win = real_all()[0]
            win.title = ""
            return [win]
        win = real_all()[0]
        win.title = "Untitled - Notepad"
        return [win]

    monkeypatch.setattr(FakeGW, "getAllWindows", _flaky)
    res = mgr.list_windows(pattern="notepad")
    assert res["ok"] is True and res["count"] == 1
    assert calls["n"] == 2


def test_guess_app_aliases():
    assert WindowManager.guess_app("Untitled - Notepad") == "Notepad"
    assert WindowManager.guess_app("New Tab - Google Chrome") == "Chrome"
    assert WindowManager.guess_app("My file - Visual Studio Code") == "VS Code"


def test_resolve_exact_single_ambiguous_missing(monkeypatch):
    mgr = make_windows(monkeypatch, ["Untitled - Notepad",
                                     "Report - Notepad", "New Tab - Chrome"])
    exact = mgr.resolve("Untitled - Notepad")
    assert exact["found"] is True and exact["ambiguous"] is False

    single = mgr.resolve("chrome")
    assert single["found"] is True
    assert single["window"]["title"] == "New Tab - Chrome"

    # Two live Notepad windows: refuse, do not pick the first blindly.
    amb = mgr.resolve("notepad")
    assert amb["found"] is False and amb["ambiguous"] is True
    assert amb["reason"] == "ambiguous"
    assert len(amb["candidates"]) == 2

    missing = mgr.resolve("no such app xyz")
    assert missing["found"] is False
    assert missing["reason"] == "not_found"

    bad = mgr.resolve("   ")
    assert bad["ok"] is False and bad["reason"] == "invalid_target"


# ---------------------------------------------------------------------------
# UNIT — focus / switch / state / geometry / health / close
# ---------------------------------------------------------------------------

def test_focus_verifies_foreground_hwnd(monkeypatch):
    mgr = make_windows(monkeypatch, ["Untitled - Notepad", "New Tab - Chrome"],
                       active="first")
    res = mgr.focus("chrome")
    assert res["ok"] is True
    assert res["window"]["title"] == "New Tab - Chrome"


def test_focus_fails_cleanly(monkeypatch):
    mgr = make_windows(monkeypatch, ["Untitled - Notepad"])
    res = mgr.focus("csv viewer xyz")
    assert res["ok"] is False and res["reason"] == "not_found"

    amb = mgr.focus("notepad")
    # Single window here; force ambiguity with a twin.
    FakeGW.state["windows"].append(
        FakeWin(FakeGW.state, 777, "Other - Notepad"))
    amb = mgr.focus("notepad")
    assert amb["ok"] is False and amb["reason"] == "ambiguous"


def test_switch_recent_alt_tab(monkeypatch):
    from tools import windows as windows_mod

    class FakePyAuto:
        def __init__(self):
            self.calls = []

        def keyDown(self, key):
            self.calls.append(("down", key))

        def press(self, key):
            self.calls.append(("press", key))
            # Alt+Tab moves to the other fake window.
            wins = FakeGW.state["windows"]
            cur = FakeGW.state["active"]
            others = [w for w in wins if w._hWnd != cur]
            if others:
                FakeGW.state["active"] = others[0]._hWnd

        def keyUp(self, key):
            self.calls.append(("up", key))

    mgr = make_windows(monkeypatch, ["A - Notepad", "B - Chrome"],
                       active="first")
    before = FakeGW.state["active"]
    monkeypatch.setattr(windows_mod, "pyautogui", FakePyAuto())
    res = mgr.switch_recent()
    assert res["ok"] is True
    assert res["strategy"] == "alt_tab"
    assert FakeGW.state["active"] != before


def test_set_state_verified(monkeypatch):
    mgr = make_windows(monkeypatch, ["Untitled - Notepad"], active="first")
    minimized = mgr.set_state("notepad", "minimize")
    assert minimized["ok"] is True
    assert minimized["window"]["minimized"] is True

    maximized = mgr.set_state("notepad", "maximize")
    assert maximized["ok"] is True
    assert maximized["window"]["maximized"] is True

    restored = mgr.set_state("notepad", "restore")
    assert restored["ok"] is True
    assert restored["window"]["minimized"] is False

    bad = mgr.set_state("notepad", "spin")
    assert bad["ok"] is False and bad["reason"] == "invalid_state"


def test_move_resize_verified_and_rejected(monkeypatch):
    mgr = make_windows(monkeypatch, ["Untitled - Notepad"], active="first")
    res = mgr.move_resize("notepad", 100, 100, 900, 700)
    assert res["ok"] is True
    assert res["window"]["rect"] == {"x": 100, "y": 100,
                                     "width": 900, "height": 700}
    assert res["size_exact"] is True

    for bad in [("notepad", -1, 0, 50, 50), ("notepad", 0, 0, 0, 50)]:
        res = mgr.move_resize(*bad)
        assert res["ok"] is False and res["reason"] == "invalid_geometry"


def test_snap_left_right(monkeypatch):
    mgr = make_windows(monkeypatch, ["Untitled - Notepad"], active="first")
    left = mgr.snap("notepad", "left")
    assert left["ok"] is True
    assert left["window"]["rect"]["x"] == 0
    right = mgr.snap("notepad", "right")
    assert right["ok"] is True
    assert right["window"]["rect"]["x"] == 960
    bad = mgr.snap("notepad", "up")
    assert bad["ok"] is False and bad["reason"] == "invalid_side"


def test_arrange_side_by_side(monkeypatch):
    mgr = make_windows(monkeypatch, ["A - Notepad", "B - Notepad"])
    res = mgr.arrange("A - Notepad", "B - Notepad")
    assert res["ok"] is True
    assert res["left"]["rect"]["x"] == 0
    assert res["right"]["rect"]["x"] == 960


def test_health_responsive_and_hung(monkeypatch):
    from tools import windows as windows_mod
    mgr = make_windows(monkeypatch, ["Untitled - Notepad", "Stuck - App"])
    monkeypatch.setattr(windows_mod, "_is_hung", lambda hwnd: False)
    ok = mgr.health("notepad")
    assert ok["ok"] is True and ok["responsive"] is True
    assert ok["probed"] is True

    # Kernel says hung: unresponsive even though the SMT round-trip
    # answers (the WM_NULL result word is 0 when healthy too).
    monkeypatch.setattr(windows_mod, "_is_hung", lambda hwnd: True)
    hung = mgr.health("notepad")
    assert hung["ok"] is True and hung["responsive"] is False

    gone = mgr.health("missing app xyz")
    assert gone["ok"] is False

    # Unprobeable platform: optimistic, marked unprobed.
    monkeypatch.setattr(windows_mod, "_is_hung", lambda hwnd: None)
    soft = mgr.health("notepad")
    assert soft["ok"] is True and soft["probed"] is False


def test_close_window_verified_gone(monkeypatch):
    mgr = make_windows(monkeypatch, ["Untitled - Notepad"], active="first")
    res = mgr.close_window("notepad", timeout=3.0)
    assert res["ok"] is True
    assert FakeGW.state["windows"] == []

    again = mgr.close_window("notepad")
    assert again["ok"] is False and again["reason"] == "not_found"


def test_restart_app_needs_launcher(monkeypatch):
    mgr = make_windows(monkeypatch, ["Untitled - Notepad"])
    mgr.desktop = None
    res = mgr.restart_app("notepad")
    assert res["ok"] is False and res["reason"] == "restart_unsupported"


def test_restart_app_relaunches(monkeypatch):
    mgr = make_windows(monkeypatch, ["Untitled - Notepad"], active="first")
    desktop = FakeDesktop()
    mgr.desktop = desktop
    # wait_for_window is driven by FakeComputer against FakeGW titles:
    # simulate the relaunched window appearing.
    orig_open = desktop.open_app

    def _open(app):
        orig_open(app)
        FakeGW.state["windows"].append(
            FakeWin(FakeGW.state, 555, "Untitled - Notepad"))
        return True

    desktop.open_app = _open
    res = mgr.restart_app("notepad", app="notepad", timeout=5.0)
    assert res["ok"] is True and res["app"] == "notepad"


# ---------------------------------------------------------------------------
# UNIT — workflow engine: templates, handoff, expects, recovery
# ---------------------------------------------------------------------------

def _runner(results):
    def _run(step):
        tool = step["tool"]
        if callable(results.get(tool)):
            return results[tool](step)
        return results.get(tool, ToolResult(tool, True, "ok",
                                            {"started": True,
                                             "completed": True}))
    return _run


def test_template_resolution():
    engine = WorkflowEngine(step_runner=_runner({}))
    context = {"clipboard": "hello",
               "extracts": {"notes": {"text": "abc"}},
               "steps": {"0": {"data": {"text": "abc"}}}}
    payload = {"text": "paste {{clipboard}} and {{extracts.notes.text}} "
                       "then {{steps.0.data.text}}!",
               "missing": "[{{nope}}]", "n": 3}
    out = engine.resolve_templates(payload, context)
    assert out["text"] == "paste hello and abc then abc!"
    assert out["missing"] == "[]"
    assert out["n"] == 3


def test_build_handoff_shape():
    hand = WorkflowEngine.build_handoff(
        "Chrome", {"url": "x"}, "Notepad", "editor",
        transformation="strip query",
        verification={"verified": True})
    assert hand == {"source_app": "Chrome",
                    "extracted_data": {"url": "x"},
                    "destination_app": "Notepad",
                    "destination_target": "editor",
                    "transformation": "strip query",
                    "verification": {"verified": True}}


def test_check_expect_kinds():
    visual = FakeVisual()
    computer = FakeComputer()
    computer.clipboard = "exact text"
    engine = WorkflowEngine(step_runner=_runner({}), visual=visual,
                            computer=computer)
    ok = ToolResult("t", True, "ok", {})
    assert engine.check_expect(None, ok)["verified"] is True

    visual.verify_result = {"verified": True, "evidence": {"a": 1}}
    seen = engine.check_expect({"kind": "text_visible", "text": "hi"}, ok)
    assert seen["verified"] is True and seen["evidence"] == {"a": 1}

    eq = engine.check_expect({"kind": "clipboard_equals",
                              "text": "  exact text\n"}, ok)
    assert eq["verified"] is True
    ne = engine.check_expect({"kind": "clipboard_non_empty"}, ok)
    assert ne["verified"] is True

    field = engine.check_expect(
        {"kind": "field_non_empty", "field": "text"},
        ToolResult("t", True, "ok", {"text": "x"}))
    assert field["verified"] is True
    missing_field = engine.check_expect(
        {"kind": "field_non_empty", "field": "text"},
        ToolResult("t", True, "ok", {}))
    assert missing_field["verified"] is False

    unknown = engine.check_expect({"kind": "telepathy"}, ok)
    assert unknown["verified"] is False


def test_run_workflow_success_with_handoff():
    calls = []

    def _read_clipboard(step):
        calls.append(step)
        return ToolResult("read_clipboard", True, "read",
                          {"started": True, "completed": True,
                           "text": "hello handoff"})

    engine = WorkflowEngine(step_runner=_runner({
        "switch_app": ToolResult("switch_app", True, "switched",
                                 {"started": True, "completed": True}),
        "read_clipboard": _read_clipboard,
        "visual_type": lambda step: ToolResult(
            "visual_type", True, "typed",
            {"started": True, "completed": True,
             "echo": step.get("text")}),
    }), visual=FakeVisual(), computer=FakeComputer())
    result = engine.run_workflow("move text", [
        {"tool": "switch_app", "target": "Notepad A"},
        {"tool": "read_clipboard", "save_as": "grabbed"},
        {"tool": "visual_type", "text": "{{extracts.grabbed.text}}",
         "expect": {"kind": "success"}},
    ])
    assert result.success is True
    assert result.data["verified"] is True
    assert result.data["extracts"]["grabbed"]["text"] == "hello handoff"
    assert calls and result.data["retries"] == 0
    assert [s["tool"] for s in result.data["stages"]] == [
        "switch_app", "read_clipboard", "visual_type"]


def test_run_workflow_recovers_with_refocus(monkeypatch):
    mgr = make_windows(monkeypatch, ["A - Notepad", "B - Notepad"],
                       active="first")
    attempts = []

    def _flaky(step):
        attempts.append(step["tool"])
        if len(attempts) == 1:
            return ToolResult("visual_click", False, "missed",
                              {"started": True, "completed": False})
        return ToolResult("visual_click", True, "clicked",
                          {"started": True, "completed": True})

    visual = FakeVisual()
    engine = WorkflowEngine(step_runner=_runner({"visual_click": _flaky}),
                            visual=visual, windows=mgr,
                            computer=FakeComputer())
    result = engine.run_workflow("click thing", [
        {"tool": "visual_click", "target": "Save", "app": "B - Notepad"},
    ], max_retries=1)
    assert result.success is True
    assert result.data["retries"] == 1
    stage = result.data["stages"][0]
    assert stage["recovery"]["strategy"] == "refocus"
    assert FakeGW.state["active"] == 101  # second window focused


def test_run_workflow_retry_bound():
    engine = WorkflowEngine(
        step_runner=_runner({"bad": ToolResult(
            "bad", False, "nope",
            {"started": True, "completed": False})}),
        visual=FakeVisual())
    result = engine.run_workflow("doomed", [{"tool": "bad"}],
                                 max_retries=2)
    assert result.success is False
    assert result.data["retries"] == 2
    assert result.data["stages"][0]["retries"] == 2
    assert result.data["verified"] is False


def test_run_workflow_rejects_bad_plans():
    engine = WorkflowEngine(step_runner=_runner({}))
    assert engine.run_workflow("", []).success is False
    assert engine.run_workflow("x", []).success is False
    assert engine.run_workflow("x", [{"nope": 1}]).success is False
    assert engine.run_workflow(
        "x", [{"tool": "t"} for _ in range(51)]).success is False


def test_workflow_lessons():
    engine = WorkflowEngine(step_runner=_runner({}))
    good = ToolResult("run_workflow", True, "done",
                      {"verified": True, "stages": [{"tool": "switch_app"}],
                       "retries": 0})
    lesson = WorkflowEngine.build_lesson("move text", good)
    assert lesson["outcome"] == "success"
    assert lesson["scenario"].startswith("workflow:")

    saved = []
    memory = type("M", (), {"save_experience": lambda self, **k: saved.append(k)})()
    engine.record_lesson(memory, "move text", good)
    assert saved and saved[0]["outcome"] == "success"
    engine.record_lesson(object(), "move text", good)  # tolerated


# ---------------------------------------------------------------------------
# INTEGRATION — executor dispatch + validation
# ---------------------------------------------------------------------------

def _executor_with_fakes(monkeypatch):
    from tools.executor import TaskExecutor
    from tools.visual import VisualAgent
    install_fakes(monkeypatch)
    FakeGW.reset([], None)
    ex = TaskExecutor.__new__(TaskExecutor)
    computer = FakeComputer()
    ex.computer = computer
    ex.visual = VisualAgent(computer=computer, vision=None, uia=None)
    from tools.windows import WindowManager as WM
    from tools.workflows import WorkflowEngine as WE
    ex.windows = WM(computer=computer, desktop=FakeDesktop())
    ex.workflows = WE(
        step_runner=lambda step: ex._execute_step(step["tool"], step),
        visual=ex.visual, windows=ex.windows, computer=computer)
    return ex


def test_executor_window_tools(monkeypatch):
    ex = _executor_with_fakes(monkeypatch)
    FakeGW.state["windows"] = [FakeWin(FakeGW.state, 200, "A - Notepad"),
                               FakeWin(FakeGW.state, 201, "B - Chrome")]

    listed = ex._execute_step("list_windows", {"tool": "list_windows"})
    assert listed.success is True and listed.data["count"] == 2

    switched = ex._execute_step("switch_app",
                                {"tool": "switch_app", "target": "chrome"})
    assert switched.success is True
    assert switched.data["verified"] is True
    assert switched.data["app"] == "Chrome"

    missing = ex._execute_step("switch_app",
                               {"tool": "switch_app",
                                "target": "nope xyz"})
    assert missing.success is False

    maximized = ex._execute_step(
        "window_manage",
        {"tool": "window_manage", "target": "A - Notepad",
         "action": "maximize"})
    assert maximized.success is True
    assert maximized.data["verified"] is True

    ex.computer.clipboard = "hand me over"
    read = ex._execute_step("read_clipboard", {"tool": "read_clipboard"})
    assert read.success is True
    assert read.data["text"] == "hand me over"


def test_executor_run_workflow_plan(monkeypatch):
    ex = _executor_with_fakes(monkeypatch)
    FakeGW.state["windows"] = [FakeWin(FakeGW.state, 300, "A - Notepad")]
    ex.computer.clipboard = "workflow text"
    result = ex.execute({"goal": "transfer", "steps": [{
        "tool": "run_workflow", "goal": "transfer",
        "steps": [
            {"tool": "switch_app", "target": "A - Notepad"},
            {"tool": "read_clipboard", "save_as": "grab",
             "expect": {"kind": "clipboard_non_empty"}},
        ]}]})
    assert result.success is True
    assert result.data["verified"] is True


def test_executor_window_validation_errors(monkeypatch):
    ex = _executor_with_fakes(monkeypatch)
    cases = [
        ("list_windows", {"tool": "list_windows", "pattern": "  "}),
        ("switch_app", {"tool": "switch_app", "target": ""}),
        ("window_manage", {"tool": "window_manage", "target": "x"}),
        ("window_manage", {"tool": "window_manage", "target": "x",
                           "action": "levitate"}),
        ("window_manage", {"tool": "window_manage", "target": "x",
                           "action": "move_resize", "x": 0, "y": 0,
                           "width": 10}),
        ("extract_window_text", {"tool": "extract_window_text",
                                 "method": "telepathy"}),
        ("run_workflow", {"tool": "run_workflow", "goal": "x"}),
        ("run_workflow", {"tool": "run_workflow", "goal": "x",
                          "steps": [{"tool": 5}]}),
    ]
    for tool, step in cases:
        res = ex._execute_step(tool, step)
        assert res.success is False, (tool, step)


# ---------------------------------------------------------------------------
# INTEGRATION — planner schemas, permissions, registry, server phases
# ---------------------------------------------------------------------------

def test_planner_accepts_window_tools():
    from agents.planner import TaskPlanner
    TaskPlanner._validate_plan({"goal": "windows", "steps": [
        {"tool": "list_windows", "pattern": "Chrome"},
        {"tool": "switch_app", "target": "Chrome"},
        {"tool": "window_manage", "target": "Notepad",
         "action": "maximize"},
        {"tool": "window_manage", "target": "Notepad",
         "action": "move_resize", "x": 0, "y": 0,
         "width": 960, "height": 1080},
        {"tool": "extract_window_text", "method": "clipboard"},
        {"tool": "read_clipboard"},
        {"tool": "run_workflow", "goal": "g",
         "steps": [{"tool": "list_windows"}], "max_retries": 0},
    ]})


def test_planner_rejects_bad_window_payloads():
    from agents.planner import TaskPlanner
    from agents.ollama_errors import PlannerValidationError
    bad_steps = [
        {"tool": "switch_app"},
        {"tool": "window_manage", "target": "x"},
        {"tool": "window_manage", "target": "x", "action": "maximize",
         "x": True},
        {"tool": "run_workflow", "goal": "g", "steps": "now"},
        {"tool": "run_workflow", "goal": "g",
         "steps": [{"tool": "list_windows"}], "max_retries": -1},
    ]
    for step in bad_steps:
        with pytest.raises(PlannerValidationError):
            TaskPlanner._validate_plan({"goal": "w", "steps": [step]})


def test_window_permission_levels():
    from core.permission import PermissionEngine, PermissionLevel
    engine = PermissionEngine()
    for tool in ("list_windows", "read_clipboard"):
        level, confirm = engine.evaluate(tool, {})
        assert level == PermissionLevel.HARMLESS
        assert confirm is False
    ocr_level, _ = engine.evaluate("extract_window_text",
                                   {"method": "ocr"})
    assert ocr_level == PermissionLevel.HARMLESS
    clip_level, clip_confirm = engine.evaluate(
        "extract_window_text", {"method": "clipboard"})
    assert clip_level == PermissionLevel.NORMAL
    assert clip_confirm is False
    for tool in ("switch_app", "window_manage", "run_workflow"):
        level, confirm = engine.evaluate(tool, {"target": "x"})
        assert level == PermissionLevel.NORMAL
        assert confirm is False


def test_registry_lists_window_tools():
    from backend.tools import build_default_tool_registry
    registry = build_default_tool_registry()
    expected = {"list_windows": True, "switch_app": False,
                "window_manage": False, "extract_window_text": True,
                "read_clipboard": True, "run_workflow": False}
    for name, idempotent in expected.items():
        definition = registry.get(name)
        assert definition is not None, name
        assert definition.idempotent is idempotent, name


def test_server_accepts_window_phases():
    from backend import server
    for phase in ("WINDOW_SWITCH", "APP_FOCUS", "EXTRACTING",
                  "TRANSFERRING"):
        assert phase in server.VALID_PHASES
    for tool in ("list_windows", "switch_app", "window_manage",
                 "extract_window_text", "read_clipboard", "run_workflow"):
        assert tool in server._VISUAL_TOOL_PHASES
