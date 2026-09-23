"""Stabilization-pass regression tests (M1-M4 hardening).

Every test below pins a bug found during the stabilization audit, so a
future change that reintroduces the glitch fails loudly:

- window state polling (no single-sample races), hwnd-pinned focus
  across title churn, layout abstraction, snap top/bottom
- visual ambiguity refusal, confidence tiers, re-locate on retry,
  visual_menu open/close + validation
- filesystem known-folder resolution + truthful messages
- scheduler targeted cancel + validation
- emergency-stop pending cleanup, _speak error preservation
- workflow unresolved-template trail honesty
- planner acceptance of the new menu/layout tools
"""

import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Window fakes (local: independent from other suites)
# ---------------------------------------------------------------------------


class _Win:
    def __init__(self, state, hwnd, title, rect=(0, 0, 800, 600)):
        self._state = state
        self._hWnd = hwnd
        self.title = title
        self.left, self.top, self.width, self.height = rect
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


class _SlowMinWin(_Win):
    """DWM-style animation: minimized flag flips after two reads."""

    def __init__(self, *args):
        super().__init__(*args)
        self._reads = 0

    @property
    def isMinimized(self):
        self._reads += 1
        return self._reads > 2


class _GW:
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


def _install_gw(monkeypatch):
    from tools import windows as windows_mod
    monkeypatch.setattr(windows_mod, "gw", _GW)
    fake_w32 = types.ModuleType("win32gui")
    fake_w32.IsWindow = lambda hwnd: any(
        w._hWnd == hwnd for w in _GW.state["windows"])
    fake_w32.SetForegroundWindow = lambda hwnd: _GW.state.update(
        active=hwnd)
    fake_w32.SendMessageTimeout = lambda *a: (1, 1)
    fake_w32.PostMessage = lambda hwnd, msg, w, l: _GW.state.update(
        windows=[x for x in _GW.state["windows"] if x._hWnd != hwnd])
    monkeypatch.setitem(sys.modules, "win32gui", fake_w32)
    fake_con = types.ModuleType("win32con")
    fake_con.WM_NULL = 0
    fake_con.SMTO_ABORTIFHUNG = 2
    fake_con.WM_CLOSE = 16
    monkeypatch.setitem(sys.modules, "win32con", fake_con)


class _Computer:
    def __init__(self):
        self.calls = []
        self.clipboard = ""
        self.active_title = "Untitled - Notepad"

    def screen_size(self):
        return {"width": 1920, "height": 1080}

    def get_active_window(self):
        return self.active_title

    def move(self, x, y, duration=0.2):
        self.calls.append(("move", x, y))

    def click(self, x=None, y=None):
        self.calls.append(("click", x, y))

    def right_click(self, x=None, y=None):
        self.calls.append(("right_click", x, y))

    def double_click(self, x=None, y=None):
        self.calls.append(("double_click", x, y))

    def drag(self, x, y, duration=0.4):
        self.calls.append(("drag", x, y))

    def scroll(self, amount):
        self.calls.append(("scroll", amount))

    def type_text(self, text, interval=0.02):
        self.calls.append(("type_text", text))

    def focus_window(self, title):
        self.calls.append(("focus_window", title))
        matches = _GW.getWindowsWithTitle(title)
        if not matches:
            return False
        _GW.state["active"] = matches[0]._hWnd
        return True

    def focus_hwnd(self, hwnd):
        self.calls.append(("focus_hwnd", hwnd))
        if any(w._hWnd == hwnd for w in _GW.state["windows"]):
            _GW.state["active"] = hwnd
            return True
        return False

    def wait_for_window(self, title, timeout=10):
        return bool(_GW.getWindowsWithTitle(title))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", keys))
        if keys[:1] == ("win",) and keys[1:] in (("left",), ("right",)):
            active = _GW.getActiveWindow()
            if active is not None:
                if keys[1] == "left":
                    active.moveTo(0, 0)
                else:
                    active.moveTo(960, 0)
                active.resizeTo(960, 1080)

    def press(self, key):
        self.calls.append(("press", key))

    def get_clipboard(self):
        return self.clipboard

    def set_clipboard(self, text):
        self.clipboard = text


def _manager(monkeypatch, titles, active_first=True):
    _install_gw(monkeypatch)
    wins = [_Win(_GW.state, 100 + i, t) for i, t in enumerate(titles)]
    _GW.reset(wins, wins[0]._hWnd if wins and active_first else None)
    from tools.windows import WindowManager
    return WindowManager(computer=_Computer())


# ---------------------------------------------------------------------------
# Window state polling + hwnd pins + layouts
# ---------------------------------------------------------------------------

def test_set_state_polls_past_animation(monkeypatch):
    from tools import windows as windows_mod
    _install_gw(monkeypatch)
    win = _SlowMinWin(_GW.state, 100, "App - Chrome")
    _GW.reset([win], 100)
    from tools.windows import WindowManager
    mgr = WindowManager(computer=_Computer())
    res = mgr.set_state("chrome", "minimize")
    assert res["ok"] is True
    assert res["window"]["minimized"] is True


def test_focus_survives_title_churn(monkeypatch):
    mgr = _manager(monkeypatch, ["Inbox - Google Chrome"], active_first=False)
    # The tab navigates (title churns) between resolve and focus; the
    # handle pin must still land on the right window.
    win = _GW.state["windows"][0]
    win.title = "Totally Different - Google Chrome"
    res = mgr.focus("chrome")
    assert res["ok"] is True
    assert _GW.state["active"] == win._hWnd
    assert ("focus_hwnd", win._hWnd) in mgr.computer.calls


def test_layout_rects_cover_halves():
    from tools.windows import layout_rect, rects_match
    work = {"x": 0, "y": 0, "width": 1920, "height": 1040}
    left = layout_rect("left", work)
    right = layout_rect("right", work)
    top = layout_rect("top", work)
    bottom = layout_rect("bottom", work)
    assert left == {"x": 0, "y": 0, "width": 960, "height": 1040}
    assert right["x"] == 960 and right["width"] == 960
    assert top == {"x": 0, "y": 0, "width": 1920, "height": 520}
    assert bottom["y"] == 520 and bottom["height"] == 520
    assert layout_rect("diagonal", work) is None
    assert rects_match({"x": 2, "y": 0, "width": 955, "height": 1040},
                       left) is True
    assert rects_match({"x": 400, "y": 0, "width": 960, "height": 1040},
                       left) is False


def test_snap_top_bottom_use_layout_geometry(monkeypatch):
    from tools.windows import work_area
    mgr = _manager(monkeypatch, ["Untitled - Notepad"], active_first=True)
    half = work_area(mgr.computer)["height"] // 2
    assert half > 0
    top = mgr.snap("notepad", "top")
    assert top["ok"] is True
    assert top["window"]["rect"]["height"] == half
    bottom = mgr.snap("notepad", "bottom")
    assert bottom["ok"] is True
    assert bottom["window"]["rect"]["height"] == \
        work_area(mgr.computer)["height"] - half
    bad = mgr.snap("notepad", "diagonal")
    assert bad["ok"] is False


def test_arrange_checks_both_rectangles(monkeypatch):
    mgr = _manager(monkeypatch, ["A - Notepad", "B - Notepad"])
    res = mgr.arrange("A - Notepad", "B - Notepad")
    assert res["ok"] is True
    assert res["left"]["rect"]["x"] == 0
    assert res["right"]["rect"]["x"] == 960
    assert "layout" in res


# ---------------------------------------------------------------------------
# Visual: ambiguity, tiers, re-locate, menus
# ---------------------------------------------------------------------------

from PIL import Image


def _img(color="white"):
    return Image.new("RGB", (100, 100), color)


class _Vision:
    def __init__(self):
        self.shots = [_img("white")]
        self.index = 0
        self.elements = []
        self.bounds = (0, 0, 800, 600)

    def capture_shot(self, mode="active", region=None):
        image = self.shots[min(self.index, len(self.shots) - 1)]
        self.index += 1
        w, h = image.size
        return {"ok": True, "image": image, "origin": (0, 0),
                "width": w, "height": h}

    def read_shot_elements(self, image, origin=(0, 0), min_conf=50):
        return [dict(e) for e in self.elements]

    def window_bounds(self):
        return self.bounds


class _UIA:
    def __init__(self):
        self.controls = []

    def safe_inspect(self):
        return [dict(c) for c in self.controls]


def _ocr(text, cx=100, cy=200, conf=90):
    return {"text": text, "x": cx - 10, "y": cy - 5, "w": 20, "h": 10,
            "cx": cx, "cy": cy, "confidence": conf}


def _agent():
    from tools.visual import VisualAgent
    return VisualAgent(computer=_Computer(), vision=_Vision(), uia=_UIA())


def test_locate_refuses_ambiguous_twins():
    agent = _agent()
    agent.vision.elements = [_ocr("Save", cx=100, cy=200),
                             _ocr("Save", cx=400, cy=200)]
    res = agent.locate("Save")
    assert res["found"] is False
    assert res["reason"] == "ambiguous"
    assert len(res["candidates"]) == 2


def test_locate_confidence_tiers():
    agent = _agent()
    agent.uia.controls = [{"name": "Save", "type": "Button",
                           "left": 80, "top": 190,
                           "right": 120, "bottom": 210}]
    res = agent.locate("Save")
    assert res["found"] is True
    assert res["located"]["confidence_level"] == "high"

    agent2 = _agent()
    agent2.vision.elements = [_ocr("Sav", cx=100, cy=200, conf=55)]
    res2 = agent2.locate("Save", min_confidence=0.5)
    assert res2["found"] is True
    assert res2["located"]["confidence"] < 0.85
    assert res2["located"]["confidence_level"] == "low"


def test_visual_click_relocates_on_retry():
    agent = _agent()
    agent.vision.elements = [_ocr("Save", cx=100, cy=200)]
    agent.vision.shots = [_img("white")] * 4 + [_img("black")] * 4
    calls = []
    real_locate = agent.locate

    def _moving_locate(target, min_confidence=0.6, mode="active"):
        out = real_locate(target, min_confidence, mode)
        if out.get("found"):
            # The menu re-renders 60px lower after the first miss.
            out["located"] = dict(out["located"],
                                  point={"x": 160, "y": 200})
        calls.append(out.get("found"))
        return out

    agent.locate = _moving_locate
    res = agent.visual_click("Save", max_retries=1)
    assert res["ok"] is True
    assert res.get("relocated") is True
    assert res["located"]["point"] == {"x": 160, "y": 200}


def _uia_menu(name, cx=100, cy=40):
    return {"name": name, "type": "MenuItem",
            "left": cx - 20, "top": cy - 8,
            "right": cx + 20, "bottom": cy + 8}


def test_visual_menu_open_verified():
    agent = _agent()
    agent.uia.controls = [_uia_menu("File")]
    opened = agent.visual_menu("the File menu", action="open")
    # No new items appear in the fake: honest failure, not a bare claim.
    assert opened["ok"] is False
    assert opened["reason"] == "menu_not_observed"

    agent2 = _agent()
    agent2.uia.controls = [_uia_menu("File")]
    real_sig = agent2._menu_signature
    states = [{"labels": frozenset({"File"}), "n": 1},
              {"labels": frozenset({"File", "New", "Open"}), "n": 4}]

    def _growing_sig():
        state = states[min(agent2._sig_calls[0],
                           len(states) - 1)]
        agent2._sig_calls[0] += 1
        return state["labels"], state["n"], "Notepad"

    agent2._sig_calls = [0]
    agent2._menu_signature = _growing_sig
    res = agent2.visual_menu("the File menu", action="open")
    assert res["ok"] is True and res["verified"] is True
    assert "New" in res["evidence"]["menu_check"]["new_items"]
    assert real_sig is not None


def test_visual_menu_close_strategies():
    agent = _agent()
    agent.uia.controls = [_uia_menu("File")]
    res = agent.visual_menu("the File menu", action="close")
    # Escape on an already-closed menu verifies immediately.
    assert res["ok"] is True and res["verified"] is True
    assert res["evidence"]["attempts"][0]["strategy"] == "escape"

    bad = agent.visual_menu("the File menu", action="explode")
    assert bad["ok"] is False


def test_visual_menu_validation_in_executor(monkeypatch):
    from tools.executor import TaskExecutor
    from tools import windows as windows_mod
    _install_gw(monkeypatch)
    _GW.reset([], None)
    ex = TaskExecutor.__new__(TaskExecutor)
    ex.visual = _agent()
    res = ex._execute_step("visual_menu",
                           {"tool": "visual_menu", "target": "  "})
    assert res.success is False
    res = ex._execute_step("visual_menu",
                           {"tool": "visual_menu", "target": "File",
                            "action": "wiggle"})
    assert res.success is False


# ---------------------------------------------------------------------------
# Filesystem known folders + truthful messages
# ---------------------------------------------------------------------------

def test_resolve_user_path_aliases(monkeypatch, tmp_path):
    import pathlib
    from tools import filesystem as fs_mod
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    assert fs_mod.resolve_user_path("my Downloads folder") == \
        (home / "Downloads").resolve()
    assert fs_mod.resolve_user_path("home") == home.resolve()
    assert fs_mod.resolve_user_path("") is None
    assert fs_mod.resolve_user_path("zzz-nope-xyz",
                                    must_exist=True) is None
    assert fs_mod.display_path(home / "Downloads") == \
        "~" + str(home / "Downloads")[len(str(home)):]


def test_resolve_paths_rooted_at_known_folders(monkeypatch, tmp_path):
    import pathlib
    from tools import filesystem as fs_mod
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    assert fs_mod.resolve_user_path("Desktop/report.pdf") == \
        home / "Desktop" / "report.pdf"
    assert fs_mod.resolve_user_path("my Downloads/x.zip") == \
        home / "Downloads" / "x.zip"
    assert fs_mod.resolve_user_path("Desktop/missing.pdf",
                                    must_exist=True) is None


def test_locate_failure_message_is_human():
    from tools.executor import TaskExecutor
    ex = TaskExecutor.__new__(TaskExecutor)
    ex.visual = _agent()
    res = ex._execute_step("locate_target",
                           {"tool": "locate_target",
                            "target": "NoSuchThingXYZ"})
    assert res.success is False
    assert "couldn't confidently find" in res.message
    assert res.data["reason"] == "low_confidence"


def test_filesystem_messages_name_resolved_paths(tmp_path):
    from tools.filesystem import FilesystemTools
    target = tmp_path / "notes.txt"
    created = FilesystemTools.create_file(str(target), "hi")
    assert created.success is True and created.data["verified"] is True
    assert "notes.txt" in created.message
    listed = FilesystemTools.list_files(str(tmp_path))
    assert listed.success is True and listed.message.startswith("Listed 1 items in ~")
    missing = FilesystemTools.list_files(str(tmp_path / "nope"))
    assert missing.success is False


# ---------------------------------------------------------------------------
# Scheduler targeted cancel + server stop/speak behavior
# ---------------------------------------------------------------------------

def _server(monkeypatch):
    from backend.server import JarvisBackendService
    service = JarvisBackendService()
    monkeypatch.setattr(service, "emit", lambda payload: None)
    return service


def test_targeted_cancel_keeps_others(monkeypatch):
    import tempfile
    from pathlib import Path
    from core.scheduler import TaskScheduler, parse_schedule_request
    service = _server(monkeypatch)

    class _Audio:
        def __init__(self):
            self.spoken = []

        def speak(self, text):
            self.spoken.append(text)

        def wait(self):
            pass

    with tempfile.TemporaryDirectory() as tmp:
        sched = TaskScheduler(db_path=Path(tmp) / "s.db")
        try:
            audio = _Audio()
            sched.schedule_task("reminder", {"remind": "call mom"},
                                delay_seconds=600)
            sched.schedule_task("reminder", {"remind": "drink water"},
                                delay_seconds=600)
            parsed = parse_schedule_request("cancel my drink water reminder")
            assert parsed["target"] != ""
            handled = service._handle_schedule_command(
                {"scheduler": sched, "audio": audio},
                "cancel my drink water reminder")
            assert handled is True
            remaining = [t["payload"]["remind"]
                         for t in sched.list_scheduled_tasks()]
            assert remaining == ["call mom"]
            assert "drink water" in audio.spoken[-1]
            # Unknown target cancels nothing and says so honestly.
            handled = service._handle_schedule_command(
                {"scheduler": sched, "audio": audio},
                "cancel my dentist reminder")
            assert handled is True
            assert len(sched.list_scheduled_tasks()) == 1
            assert "couldn't find" in audio.spoken[-1]
        finally:
            sched.close()


def test_handle_stop_clears_error_state(monkeypatch):
    service = _server(monkeypatch)
    service._runtime = {}
    service._voice_mode = False
    service._set_error("stale failure")
    code, _ = service.handle_stop()
    assert code == 200
    snap = service.state.snapshot()
    assert snap["error_message"] is None
    assert snap["status"] == "idle"
    assert snap["phase"] == "IDLE"


def test_handle_stop_clears_all_pending(monkeypatch):
    service = _server(monkeypatch)

    class _Lifecycle:
        def emergency_stop(self):
            pass

    class _Audio:
        def __init__(self):
            self.stopped = 0

        def stop_speaking(self):
            self.stopped += 1

    audio = _Audio()
    service._runtime = {"lifecycle": _Lifecycle(), "audio": audio}
    service._voice_mode = False
    service._pending_clarification = {"q": 1}
    service._pending_organize = {"p": 1}
    code, payload = service.handle_stop()
    assert code == 200 and payload == {"ok": True, "stopped": True}
    assert service._pending_clarification is None
    assert service._pending_organize is None
    assert audio.stopped == 1


def test_speak_preserves_prior_error(monkeypatch):
    service = _server(monkeypatch)

    class _Audio:
        def speak(self, text):
            pass

        def wait(self):
            pass

    service._set_error("boom happened")
    service._speak(_Audio(), "boom happened")
    snap = service.state.snapshot()
    assert snap["status"] == "error"
    assert snap["phase"] == "ERROR"
    assert snap["error_message"] == "boom happened"
    # Normal speech still returns to idle.
    service.state.set_status("idle")
    service._set_phase("IDLE")
    service._speak(_Audio(), "hello there")
    assert service.state.snapshot()["status"] == "idle"


# ---------------------------------------------------------------------------
# Workflow unresolved templates + planner acceptance
# ---------------------------------------------------------------------------

def test_workflow_records_unresolved_templates():
    from backend.interfaces import ToolResult
    from tools.workflows import WorkflowEngine
    seen = []

    def _run(step):
        seen.append(step)
        return ToolResult(step["tool"], True, "ok",
                          {"started": True, "completed": True})

    engine = WorkflowEngine(step_runner=_run)
    result = engine.run_workflow("x", [
        {"tool": "type", "text": "hi {{extracts.missing}} bye"},
    ])
    assert result.success is True
    assert seen[0]["text"] == "hi  bye"
    assert result.data["stages"][0]["unresolved_templates"] == \
        ["extracts.missing"]


def test_failure_messages_are_human():
    from tools.executor import TaskExecutor
    fn = TaskExecutor._human_target_failure
    amb = fn("Chrome", {"reason": "ambiguous",
                        "candidates": [{"title": "A - Chrome"},
                                       {"title": "B - Chrome"}]}, "x")
    assert "Which one should I use?" in amb
    assert "A - Chrome" in amb
    low = fn("Save", {"reason": "low_confidence"}, "x")
    assert low == "I couldn't confidently find 'Save'."
    missing = fn("Store", {"reason": "not_found"}, "x")
    assert missing == "I couldn't find 'Store'."


def test_switch_app_ambiguity_is_spoken(monkeypatch):
    from tools.executor import TaskExecutor
    from tools import windows as windows_mod
    _install_gw(monkeypatch)
    _GW.reset([], None)
    ex = TaskExecutor.__new__(TaskExecutor)
    ex.windows = windows_mod.WindowManager(computer=_Computer())
    _GW.state["windows"] = [_Win(_GW.state, 1, "A - Chrome"),
                            _Win(_GW.state, 2, "B - Chrome")]
    res = ex._execute_step("switch_app",
                           {"tool": "switch_app", "target": "Chrome"})
    assert res.success is False
    assert "Which one should I use?" in res.message
    assert res.data["reason"] == "ambiguous"


def test_identical_titles_resolve_by_attention(monkeypatch):
    from tools import windows as windows_mod
    _install_gw(monkeypatch)
    _GW.reset([], None)
    mgr = windows_mod.WindowManager(computer=_Computer())
    _GW.state["windows"] = [_Win(_GW.state, 1, "New Tab - Chrome"),
                            _Win(_GW.state, 2, "New Tab - Chrome"),
                            _Win(_GW.state, 3, "New Tab - Chrome")]
    # Nothing focused, no history: still ambiguous.
    res = mgr.resolve("chrome")
    assert res["found"] is False and res["reason"] == "ambiguous"
    # Foreground one wins when it is among them.
    _GW.state["active"] = 2
    res = mgr.resolve("chrome")
    assert res["found"] is True
    assert res["window"]["hwnd"] == 2
    assert res["selection"] == "active_window"
    # Otherwise the most recently focused one wins.
    _GW.state["active"] = None
    mgr._remember(3)
    mgr._remember(1)
    res = mgr.resolve("chrome")
    assert res["found"] is True
    assert res["window"]["hwnd"] == 1
    assert res["selection"] == "recently_used"


def test_different_titles_stay_ambiguous(monkeypatch):
    from tools import windows as windows_mod
    _install_gw(monkeypatch)
    _GW.reset([], None)
    mgr = windows_mod.WindowManager(computer=_Computer())
    _GW.state["windows"] = [_Win(_GW.state, 1, "A - Chrome"),
                            _Win(_GW.state, 2, "B - Chrome")]
    _GW.state["active"] = 1
    res = mgr.resolve("chrome")
    assert res["found"] is False and res["reason"] == "ambiguous"


def test_resolve_bare_filename_in_known_folders(monkeypatch, tmp_path):
    import pathlib
    from tools import filesystem as fs_mod
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    (home / "Desktop" / "report.pdf").write_text("x")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(tmp_path)
    found = fs_mod.resolve_user_path("report.pdf", must_exist=True)
    assert found == (home / "Desktop" / "report.pdf").resolve()
    assert fs_mod.resolve_user_path("nope-missing.pdf",
                                    must_exist=True) is None


def test_planner_accepts_menu_and_layout_tools():
    from agents.planner import TaskPlanner
    TaskPlanner._validate_plan({"goal": "menu", "steps": [
        {"tool": "visual_menu", "target": "the File menu",
         "action": "open"},
        {"tool": "window_manage", "target": "Chrome",
         "action": "snap_top"},
        {"tool": "window_manage", "target": "Chrome",
         "action": "snap_bottom"},
    ]})


def test_planner_retries_once_on_validation_failure():
    from agents.planner import TaskPlanner
    calls = []

    class _Provider:
        def complete(self, messages):
            calls.append(len(messages))
            if len(calls) == 1:
                # Wrong-typed required field: unfixable by stripping,
                # so the feedback retry must run.
                return ('{"goal": "g", "steps": '
                        '[{"tool": "list_windows", "pattern": 42}]}')
            return ('{"goal": "g", "steps": [{"tool": "list_windows"}]}')

    planner = TaskPlanner.__new__(TaskPlanner)
    planner.provider = _Provider()
    plan = planner.create_plan("list windows")
    assert plan["steps"] == [{"tool": "list_windows"}]
    assert len(calls) == 2
    assert calls[1] > calls[0]  # feedback appended on retry


def test_planner_retry_gives_up_after_second_failure():
    from agents.planner import TaskPlanner
    from agents.ollama_errors import PlannerValidationError

    class _Provider:
        def complete(self, messages):
            return "not json at all"

    planner = TaskPlanner.__new__(TaskPlanner)
    planner.provider = _Provider()
    with pytest.raises(PlannerValidationError):
        planner.create_plan("list windows")


def test_planner_validates_nested_workflow_steps():
    from agents.planner import TaskPlanner
    from agents.ollama_errors import PlannerValidationError
    # Good nested plan passes, workflow keys allowed.
    TaskPlanner._validate_plan({"goal": "w", "steps": [
        {"tool": "run_workflow", "goal": "g", "steps": [
            {"tool": "switch_app", "target": "X",
             "expect": {"kind": "success"}, "save_as": "s"},
        ]},
    ]})
    # Unknown nested tool fails at plan time, not mid-execution.
    with pytest.raises(PlannerValidationError):
        TaskPlanner._validate_plan({"goal": "w", "steps": [
            {"tool": "run_workflow", "goal": "g", "steps": [
                {"tool": "teleport", "target": "X"},
            ]},
        ]})
    # Nested workflows cannot recurse.
    with pytest.raises(PlannerValidationError):
        TaskPlanner._validate_plan({"goal": "w", "steps": [
            {"tool": "run_workflow", "goal": "g", "steps": [
                {"tool": "run_workflow", "goal": "h", "steps": []},
            ]},
        ]})
    # Missing nested required field fails.
    with pytest.raises(PlannerValidationError):
        TaskPlanner._validate_plan({"goal": "w", "steps": [
            {"tool": "run_workflow", "goal": "g", "steps": [
                {"tool": "switch_app"},
            ]},
        ]})


def test_scheduler_prunes_old_history():
    import tempfile
    from pathlib import Path
    from datetime import datetime, timedelta, timezone
    from core.scheduler import TaskScheduler
    with tempfile.TemporaryDirectory() as tmp:
        sched = TaskScheduler(db_path=Path(tmp) / "s.db")
        try:
            old = (datetime.now(timezone.utc)
                   - timedelta(days=60)).isoformat()
            with sched.lock:
                sched.conn.execute(
                    "INSERT INTO scheduled_tasks "
                    "(id, task_name, payload_json, run_at, status,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("old1", "reminder", "{}", old, "completed", old))
                sched.conn.execute(
                    "INSERT INTO scheduled_tasks "
                    "(id, task_name, payload_json, run_at, status,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("old2", "reminder", "{}", old, "cancelled", old))
                sched.conn.commit()
            assert sched.prune_history() == 2
            tid = sched.schedule_task("reminder", {"remind": "fresh"},
                                      delay_seconds=600)
            assert sched.prune_history() == 0
            assert any(t["id"] == tid
                       for t in sched.list_scheduled_tasks())
        finally:
            sched.close()
