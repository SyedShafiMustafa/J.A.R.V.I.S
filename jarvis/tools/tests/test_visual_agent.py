"""Milestone 3 — visual computer agent tests.

Everything runs against in-memory fakes: no real screen, mouse, keyboard,
OCR or UIA tree is touched. Real-machine validation lives in
``scripts/test_visual_validation.py`` (excluded from pytest).
"""

import pytest
from PIL import Image

from tools.visual import VisualAgent

# ---------------------------------------------------------------------------
# Fakes (in-memory screen + input devices)
# ---------------------------------------------------------------------------


def _img(color="white"):
    return Image.new("RGB", (100, 100), color)


class FakeComputer:
    def __init__(self):
        self.calls = []
        self.active_title = "Untitled - Notepad"
        self.screen = {"width": 1920, "height": 1080}
        self.clipboard = ""

    def screen_size(self):
        return dict(self.screen)

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

    def press(self, key):
        self.calls.append(("press", key))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", keys))

    def set_clipboard(self, text):
        self.clipboard = text

    def paste(self):
        self.calls.append(("paste", self.clipboard))


class FakeVision:
    def __init__(self):
        self.shots = [_img("white")]
        self.shot_index = 0
        self.fail_capture = False
        self.ocr_elements = []
        self.bounds = (0, 0, 800, 600)

    def capture_shot(self, mode="active", region=None):
        if self.fail_capture:
            return {"ok": False, "error": "no display"}
        image = self.shots[min(self.shot_index, len(self.shots) - 1)]
        self.shot_index += 1
        if mode == "region" and isinstance(region, dict):
            origin = (region["x"], region["y"])
            w, h = region["width"], region["height"]
        else:
            origin = (0, 0)
            w, h = image.size
        return {"ok": True, "image": image, "origin": origin,
                "width": w, "height": h}

    def read_shot_elements(self, image, origin=(0, 0), min_conf=50):
        return [dict(e) for e in self.ocr_elements]

    def window_bounds(self):
        return self.bounds


class FakeUIA:
    def __init__(self):
        self.controls = []

    def safe_inspect(self):
        return [dict(c) for c in self.controls]


def make_agent(**overrides):
    computer = overrides.get("computer", FakeComputer())
    vision = overrides.get("vision", FakeVision())
    uia = overrides.get("uia", FakeUIA())
    return VisualAgent(computer=computer, vision=vision, uia=uia)


def ocr_word(text, cx=100, cy=200, conf=90):
    return {"text": text, "x": cx - 10, "y": cy - 5, "w": 20, "h": 10,
            "cx": cx, "cy": cy, "confidence": conf}


def uia_button(name, cx=100, cy=200):
    return {"name": name, "type": "Button",
            "left": cx - 20, "top": cy - 10,
            "right": cx + 20, "bottom": cy + 10}


# ---------------------------------------------------------------------------
# OBSERVE — capture schema, modes, region safety, graceful failure
# ---------------------------------------------------------------------------

def test_capture_rejects_invalid_mode():
    res = make_agent().capture(mode="ultrawide")
    assert res["ok"] is False and res["reason"] == "invalid_mode"


def test_capture_full_and_active_ok():
    agent = make_agent()
    for mode in ("full", "active"):
        res = agent.capture(mode=mode)
        assert res["ok"] is True
        assert res["mode"] == mode
        assert res["width"] == 100 and res["height"] == 100
        assert res["origin"] == {"x": 0, "y": 0}
        assert "screenshot_ms" in res


def test_capture_region_clamped_to_screen():
    agent = make_agent()
    res = agent.capture(mode="region", region={
        "x": 1900, "y": 1000, "width": 5000, "height": 5000})
    assert res["ok"] is True
    assert res["width"] == 20 and res["height"] == 80


def test_capture_region_rejects_garbage():
    agent = make_agent()
    for bad in (None, "10,10", {"x": -1, "y": 0, "width": 5, "height": 5},
                {"x": 0, "y": 0, "width": 0, "height": 5},
                {"x": 5000, "y": 5000, "width": 5, "height": 5}):
        res = agent.capture(mode="region", region=bad)
        assert res["ok"] is False and res["reason"] == "invalid_region"


def test_capture_failure_is_structured():
    vision = FakeVision()
    vision.fail_capture = True
    res = make_agent(vision=vision).capture()
    assert res["ok"] is False
    assert res["reason"] == "capture_unavailable"


def test_active_window_awareness():
    res = make_agent().active_window()
    assert res == {"available": True, "title": "Untitled - Notepad",
                   "app": "Notepad",
                   "bounds": {"x": 0, "y": 0, "width": 800, "height": 600}}


def test_active_window_unavailable():
    computer = FakeComputer()
    computer.active_title = None
    res = make_agent(computer=computer).active_window()
    assert res["available"] is False and res["title"] is None


# ---------------------------------------------------------------------------
# UNDERSTAND — UIA + OCR fusion, no hallucination, uncertainty
# ---------------------------------------------------------------------------

def test_inspect_fuses_uia_and_ocr():
    vision = FakeVision()
    vision.ocr_elements = [ocr_word("Hello"), ocr_word("world")]
    uia = FakeUIA()
    uia.controls = [uia_button("Save")]
    model = make_agent(vision=vision, uia=uia).inspect()
    assert model["ok"] is True
    assert model["window"] == "Untitled - Notepad"
    assert model["app"] == "Notepad"
    by_label = {e["label"]: e for e in model["elements"]}
    assert by_label["Save"]["type"] == "button"
    assert by_label["Save"]["source"] == "uia"
    assert by_label["Save"]["confidence"] == 0.9
    assert by_label["Hello"]["type"] == "text"
    assert "Hello" in model["text"] and "world" in model["text"]
    assert model["uncertain"] is False
    assert set(model["timings"]) >= {"screenshot_ms", "uia_ms", "ocr_ms"}


def test_inspect_without_uia_is_ocr_only():
    uia = FakeUIA()
    uia.controls = [uia_button("Save")]
    vision = FakeVision()
    vision.ocr_elements = [ocr_word("Hello")]
    model = make_agent(vision=vision, uia=uia).inspect(include_uia=False)
    assert model["ok"] is True
    assert [e["label"] for e in model["elements"]] == ["Hello"]
    assert model["timings"]["uia_ms"] == 0


def test_inspect_uncertain_when_empty():
    model = make_agent().inspect()
    assert model["ok"] is True
    assert model["elements"] == []
    assert model["uncertain"] is True


def test_inspect_dedupes_overlapping_labels():
    vision = FakeVision()
    vision.ocr_elements = [ocr_word("Save", cx=100, cy=200, conf=80)]
    uia = FakeUIA()
    uia.controls = [uia_button("Save", cx=102, cy=201)]
    model = make_agent(vision=vision, uia=uia).inspect()
    saves = [e for e in model["elements"] if e["label"] == "Save"]
    assert len(saves) == 1


def test_uia_element_rejects_empty_or_inverted_rects():
    assert VisualAgent._uia_element({"name": "", "type": "Button",
                                     "left": 0, "top": 0,
                                     "right": 5, "bottom": 5}) is None
    assert VisualAgent._uia_element({"name": "X", "type": "Button",
                                     "left": 9, "top": 0,
                                     "right": 5, "bottom": 5}) is None


# ---------------------------------------------------------------------------
# LOCATE — ranking, type hints, refusal on low confidence
# ---------------------------------------------------------------------------

def test_locate_exact_button():
    uia = FakeUIA()
    uia.controls = [uia_button("Cancel", cx=10), uia_button("Save", cx=300)]
    res = make_agent(uia=uia).locate("the Save button")
    assert res["found"] is True
    assert res["located"]["label"] == "Save"
    assert res["located"]["type"] == "button"
    assert res["located"]["point"] == {"x": 300, "y": 200}
    assert res["uncertain"] is False


def test_locate_ocr_text_with_partial_words():
    vision = FakeVision()
    vision.ocr_elements = [ocr_word("File"), ocr_word("Edit")]
    res = make_agent(vision=vision).locate("the File menu")
    assert res["found"] is True
    assert res["located"]["label"] == "File"


def test_locate_refuses_low_confidence():
    vision = FakeVision()
    vision.ocr_elements = [ocr_word("SomethingElse")]
    res = make_agent(vision=vision).locate("the Save button",
                                           min_confidence=0.9)
    assert res["found"] is True or res["found"] is False
    # "SomethingElse" shares no words with "save" -> no candidates at all.
    assert res["found"] is False
    assert res["reason"] == "low_confidence"
    assert res["uncertain"] is True


def test_locate_rejects_empty_target():
    res = make_agent().locate("   ")
    assert res["ok"] is False and res["reason"] == "invalid_target"


def test_locate_reports_observe_failure():
    vision = FakeVision()
    vision.fail_capture = True
    res = make_agent(vision=vision).locate("Save")
    assert res["ok"] is False and res["reason"] == "observe_failed"


# ---------------------------------------------------------------------------
# ACT — mouse / keyboard validation and structured results
# ---------------------------------------------------------------------------

def test_click_point_validates_button_and_point():
    agent = make_agent()
    assert agent.click_point({"x": 5, "y": 5},
                             button="middle")["reason"] == "invalid_button"
    assert agent.click_point({"x": -1, "y": 5})["reason"] == "invalid_point"
    assert agent.click_point({"x": 5000, "y": 5})["reason"] == "invalid_point"


def test_click_point_dispatches_each_button():
    agent = make_agent()
    assert agent.click_point({"x": 5, "y": 5})["ok"] is True
    assert agent.click_point({"x": 5, "y": 5},
                             button="right")["ok"] is True
    assert agent.click_point({"x": 5, "y": 5},
                             button="double")["ok"] is True
    kinds = [c[0] for c in agent.computer.calls]
    assert kinds == ["click", "right_click", "double_click"]


def test_drag_rejects_bad_endpoints():
    agent = make_agent()
    res = agent.drag_points({"x": 1, "y": 1}, {"x": -4, "y": 2})
    assert res["ok"] is False and res["reason"] == "invalid_point"


def test_scroll_rejects_non_integer_amount():
    agent = make_agent()
    assert agent.scroll_at("much")["reason"] == "invalid_amount"
    assert agent.scroll_at(True)["reason"] == "invalid_amount"
    assert agent.scroll_at(-480)["ok"] is True


def test_type_text_validation_and_no_echo():
    agent = make_agent()
    assert agent.type_text("")["reason"] == "invalid_text"
    assert agent.type_text("x" * 2001)["reason"] == "text_too_long"
    bad_win = agent.type_text("hi", expected_window="Chrome")
    assert bad_win["reason"] == "wrong_window"
    assert bad_win["active_window"] == "Untitled - Notepad"

    secret = "s3cr3t-pw"
    res = agent.type_text(secret, expected_window="notepad")
    assert res["ok"] is True and res["typed_chars"] == len(secret)
    assert secret not in str(res)


def test_press_key_allowlist():
    agent = make_agent()
    assert agent.press_key("enter")["ok"] is True
    assert agent.press_key("a")["ok"] is True
    assert agent.press_key("f13")["reason"] == "invalid_key"
    assert agent.hotkeys(["ctrl", "s"])["ok"] is True
    assert agent.hotkeys([])["reason"] == "invalid_hotkey"


# ---------------------------------------------------------------------------
# VERIFY — text / window expectations with evidence
# ---------------------------------------------------------------------------

def test_verify_text_visible_and_absent():
    vision = FakeVision()
    vision.ocr_elements = [ocr_word("Hello")]
    agent = make_agent(vision=vision)
    seen = agent.verify({"kind": "text_visible", "text": "hello"})
    assert seen["verified"] is True
    assert seen["evidence"]["text_found"] is True
    missing = agent.verify({"kind": "text_visible", "text": "goodbye"})
    assert missing["verified"] is False
    absent = agent.verify({"kind": "text_absent", "text": "goodbye"})
    assert absent["verified"] is True


def test_verify_text_visible_tolerates_ocr_noise():
    vision = FakeVision()
    vision.ocr_elements = [ocr_word("JARV1S"), ocr_word("visual"),
                           ocr_word("test")]
    agent = make_agent(vision=vision)
    res = agent.verify({"kind": "text_visible",
                        "text": "JARVIS visual test"})
    assert res["verified"] is True
    assert res["evidence"]["match"].startswith("fuzzy:")
    res2 = agent.verify({"kind": "text_visible", "text": "goodbye"})
    assert res2["verified"] is False


def test_verify_text_visible_tolerates_scrambled_order():
    vision = FakeVision()
    vision.ocr_elements = [ocr_word("File"), ocr_word("Edit"),
                           ocr_word("visual"), ocr_word("JARVIS"),
                           ocr_word("test|"), ocr_word("UTF-8")]
    agent = make_agent(vision=vision)
    res = agent.verify({"kind": "text_visible",
                        "text": "JARVIS visual test"})
    assert res["verified"] is True
    assert res["evidence"]["match"].startswith("words:")


def test_verify_text_visible_rejects_scattered_words():
    vision = FakeVision()
    vision.ocr_elements = (
        [ocr_word("JARVIS")] +
        [ocr_word(f"filler{i}") for i in range(12)] +
        [ocr_word("visual")] +
        [ocr_word(f"pad{i}") for i in range(12)] +
        [ocr_word("test")]
    )
    agent = make_agent(vision=vision)
    res = agent.verify({"kind": "text_visible",
                        "text": "JARVIS visual test"})
    assert res["verified"] is False


def test_verify_window_active():
    agent = make_agent()
    assert agent.verify({"kind": "window_active",
                         "title": "notepad"})["verified"] is True
    assert agent.verify({"kind": "window_active",
                         "title": "chrome"})["verified"] is False


def test_verify_window_closed(monkeypatch):
    agent = make_agent()
    monkeypatch.setattr("tools.visual.gw.getWindowsWithTitle",
                        lambda title: ["win"] if "open" in title else [])
    assert agent.verify({"kind": "window_closed",
                         "title": "gone app"})["verified"] is True
    assert agent.verify({"kind": "window_closed",
                         "title": "open app"})["verified"] is False


def test_verify_rejects_bad_expectations():
    agent = make_agent()
    assert agent.verify({"kind": "nope"})["reason"] == "invalid_expectation"
    assert agent.verify("text")["reason"] == "invalid_expectation"
    assert agent.verify({"kind": "text_visible",
                         "text": " "})["reason"] == "invalid_expectation"


# ---------------------------------------------------------------------------
# RECOVER — bounded retries, alternation, give-up
# ---------------------------------------------------------------------------

def test_run_guarded_succeeds_first_try():
    agent = make_agent()
    res = agent.run_guarded(lambda: {"ok": True},
                            lambda: {"verified": True})
    assert res == {"ok": True, "attempts": 1, "verified": True,
                   "trail": res["trail"], "retries": 0,
                   "total_ms": res["total_ms"]}


def test_run_guarded_recovers_then_succeeds():
    agent = make_agent()
    seen = []

    def action():
        return {"ok": True, "n": len(seen)}

    states = [{"verified": False}, {"verified": True}]

    def verify():
        return {"verified": states[len(seen)]["verified"]}

    def recover(attempt, last):
        seen.append(attempt)
        return {"ok": True, "strategy": "re_observe"}

    res = agent.run_guarded(action, verify, max_retries=2,
                            recover_fn=recover)
    assert res["ok"] is True and res["attempts"] == 2
    assert res["retries"] == 1 and seen == [1]


def test_run_guarded_gives_up_after_bound():
    agent = make_agent()
    recoveries = []

    def recover(attempt, last):
        recoveries.append(attempt)
        return {"ok": True, "strategy": "re_observe"}

    res = agent.run_guarded(lambda: {"ok": True},
                            lambda: {"verified": False},
                            max_retries=2, recover_fn=recover)
    assert res["ok"] is False
    assert res["attempts"] == 3 and res["retries"] == 2
    assert recoveries == [1, 2]
    assert res["reason"] == "verify_failed_after_retries"


def test_visual_click_end_to_end_with_fakes():
    vision = FakeVision()
    vision.ocr_elements = [ocr_word("Save", cx=300, cy=200)]
    # Consumed in order: inspect capture, pre-action shot, post-action
    # shot. The last two must differ for screen-change verification.
    vision.shots = [_img("white"), _img("white"), _img("black")]
    agent = make_agent(vision=vision)
    res = agent.visual_click("the Save button")
    assert res["ok"] is True and res["verified"] is True
    assert res["located"]["label"] == "Save"
    assert ("click", 300, 200) in agent.computer.calls
    assert res["retries"] == 0


def test_visual_click_refuses_uncertain_target():
    agent = make_agent()
    res = agent.visual_click("the Save button")
    assert res["ok"] is False and res["reason"] == "low_confidence"


def test_visual_type_end_to_end_with_fakes():
    vision = FakeVision()
    vision.ocr_elements = [ocr_word("JARVIS")]
    vision.shots = [_img("white"), _img("black")]
    agent = make_agent(vision=vision)
    res = agent.visual_type("hello world", verify_text="JARVIS")
    assert res["ok"] is True and res["verified"] is True
    assert res["typed_chars"] == len("hello world")
    assert "hello world" not in str(res)


# ---------------------------------------------------------------------------
# Window focus — verified foreground handoff, honest return value
# ---------------------------------------------------------------------------

class _FakeWin:
    def __init__(self, hwnd, title, landed=True):
        self._hWnd = hwnd
        self.title = title
        self._landed = landed
        self.activate_calls = 0

    def activate(self):
        self.activate_calls += 1
        if self._landed:
            _fake_gw_state["active"] = self


_fake_gw_state = {"windows": [], "active": None}


class _FakeGW:
    @staticmethod
    def getWindowsWithTitle(title):
        return [w for w in _fake_gw_state["windows"]
                if title.lower() in w.title.lower()]

    @staticmethod
    def getActiveWindow():
        return _fake_gw_state["active"]


def _focus_controller(monkeypatch, windows, active=None,
                      set_fg_lands=True):
    from tools import computer as computer_mod
    _fake_gw_state["windows"] = windows
    _fake_gw_state["active"] = active
    monkeypatch.setattr(computer_mod, "gw", _FakeGW)

    import sys
    import types
    fake_win32 = types.ModuleType("win32gui")

    def _set_fg(hwnd):
        if set_fg_lands:
            for w in _fake_gw_state["windows"]:
                if w._hWnd == hwnd:
                    _fake_gw_state["active"] = w

    fake_win32.SetForegroundWindow = _set_fg
    monkeypatch.setitem(sys.modules, "win32gui", fake_win32)
    # tools.computer imports win32gui lazily inside focus_window, so the
    # sys.modules entry above is what it will pick up.
    from tools.computer import ComputerController
    return ComputerController()


def test_focus_returns_false_when_window_missing(monkeypatch):
    controller = _focus_controller(monkeypatch, [])
    assert controller.focus_window("Nope") is False


def test_focus_true_when_activate_lands(monkeypatch):
    win = _FakeWin(11, "Untitled - Notepad", landed=True)
    controller = _focus_controller(monkeypatch, [win], active=None)
    assert controller.focus_window("Notepad") is True
    assert win.activate_calls == 1


def test_focus_falls_back_to_win32_and_lands(monkeypatch):
    win = _FakeWin(12, "Untitled - Notepad", landed=False)
    other = _FakeWin(13, "OpenCode", landed=True)
    controller = _focus_controller(monkeypatch, [win, other],
                                   active=other, set_fg_lands=True)
    assert controller.focus_window("Notepad") is True
    assert _fake_gw_state["active"] is win


def test_focus_honest_false_when_nothing_lands(monkeypatch):
    win = _FakeWin(14, "Untitled - Notepad", landed=False)
    other = _FakeWin(15, "OpenCode", landed=True)
    controller = _focus_controller(monkeypatch, [win, other],
                                   active=other, set_fg_lands=False)
    assert controller.focus_window("Notepad") is False


# ---------------------------------------------------------------------------
# Executor wiring — dispatch + validation for the 8 visual tools
# ---------------------------------------------------------------------------

def _executor_with_visual():
    from tools.executor import TaskExecutor
    from tools.visual import VisualAgent as VA
    ex = TaskExecutor.__new__(TaskExecutor)
    ex.visual = VA(computer=FakeComputer(), vision=FakeVision(),
                   uia=FakeUIA())
    return ex


def test_executor_screenshot_inspect_locate():
    ex = _executor_with_visual()
    shot = ex._execute_step("screenshot", {"tool": "screenshot"})
    assert shot.success is True and shot.data["width"] == 100
    assert "image" not in shot.data  # images never cross the boundary

    model = ex._execute_step("inspect_screen", {"tool": "inspect_screen"})
    assert model.success is True and model.data["uncertain"] is True

    ex.visual.vision.ocr_elements = [ocr_word("Save")]
    found = ex._execute_step("locate_target",
                             {"tool": "locate_target", "target": "Save"})
    assert found.success is True
    assert found.data["target"]["label"] == "Save"

    missing = ex._execute_step("locate_target",
                               {"tool": "locate_target",
                                "target": "NopeNothing"})
    assert missing.success is False


def test_executor_visual_click_type_drag_scroll_verify():
    ex = _executor_with_visual()
    ex.visual.vision.ocr_elements = [ocr_word("Save", cx=50, cy=60),
                                     ocr_word("Folder", cx=500, cy=600)]
    # Each guarded op consumes: inspect capture, pre-action shot,
    # post-action shot (last two must differ to verify a change).
    ex.visual.vision.shots = [_img("white"), _img("white"), _img("black"),
                              _img("white"), _img("white"), _img("black")]

    clicked = ex._execute_step("visual_click",
                               {"tool": "visual_click", "target": "Save"})
    assert clicked.success is True
    assert clicked.data["verified"] is True

    typed = ex._execute_step("visual_type",
                             {"tool": "visual_type", "text": "hi"})
    assert typed.success is True

    scrolled = ex._execute_step("visual_scroll",
                                {"tool": "visual_scroll", "amount": -480})
    assert scrolled.success is True and scrolled.data["amount"] == -480

    verified = ex._execute_step(
        "visual_verify",
        {"tool": "visual_verify", "kind": "text_visible", "text": "Save"})
    assert verified.success is True and verified.data["verified"] is True


def test_executor_visual_validation_errors():
    ex = _executor_with_visual()
    cases = [
        ("screenshot", {"tool": "screenshot", "mode": "zoom"}),
        ("screenshot", {"tool": "screenshot", "mode": "region"}),
        ("visual_click", {"tool": "visual_click", "target": "Save",
                          "button": "middle"}),
        ("visual_click", {"tool": "visual_click", "target": "  "}),
        ("visual_type", {"tool": "visual_type", "text": ""}),
        ("visual_drag", {"tool": "visual_drag", "target": "a"}),
        ("visual_scroll", {"tool": "visual_scroll", "amount": True}),
        ("visual_scroll", {"tool": "visual_scroll", "amount": "far"}),
        ("visual_verify", {"tool": "visual_verify", "kind": "maybe"}),
        ("locate_target", {"tool": "locate_target",
                           "target": "x", "min_confidence": 9}),
    ]
    for tool, step in cases:
        res = ex._execute_step(tool, step)
        assert res.success is False, (tool, step)


def test_executor_visual_plan_end_to_end():
    ex = _executor_with_visual()
    ex.visual.vision.ocr_elements = [ocr_word("Save")]
    result = ex.execute({"goal": "locate save", "steps": [
        {"tool": "locate_target", "target": "Save"},
    ]})
    assert result.success is True


# ---------------------------------------------------------------------------
# Planner schemas, permission levels, registry, memory, server phases
# ---------------------------------------------------------------------------

def test_planner_accepts_visual_tools():
    from agents.planner import TaskPlanner
    steps = [
        {"tool": "screenshot", "mode": "full"},
        {"tool": "inspect_screen"},
        {"tool": "locate_target", "target": "Save",
         "min_confidence": 0.7},
        {"tool": "visual_click", "target": "Save", "button": "double"},
        {"tool": "visual_type", "text": "hi", "target": "box"},
        {"tool": "visual_drag", "target": "a", "to_target": "b"},
        {"tool": "visual_scroll", "amount": -480},
        {"tool": "visual_verify", "kind": "window_active",
         "title": "Notepad"},
        {"tool": "screenshot", "mode": "region",
         "region": {"x": 1, "y": 2, "width": 3, "height": 4}},
    ]
    TaskPlanner._validate_plan({"goal": "visual", "steps": steps})


def test_planner_rejects_bad_visual_payloads():
    from agents.planner import TaskPlanner
    from agents.ollama_errors import PlannerValidationError
    bad_steps = [
        {"tool": "visual_scroll", "amount": "far"},
        {"tool": "screenshot", "mode": "region", "region": "1,2"},
        {"tool": "locate_target"},
    ]
    for step in bad_steps:
        with pytest.raises(PlannerValidationError):
            TaskPlanner._validate_plan({"goal": "visual",
                                        "steps": [step]})


def test_planner_strips_noise_keys_and_null_optionals():
    from agents.planner import TaskPlanner
    plan = {"goal": "visual", "steps": [
        {"tool": "visual_click", "target": "x", "bogus": 1},
        {"tool": "create_file", "path": "a.txt", "content": None},
    ]}
    TaskPlanner._validate_plan(plan)
    assert plan["steps"][0] == {"tool": "visual_click", "target": "x"}
    assert plan["steps"][1] == {"tool": "create_file", "path": "a.txt"}
    # ...but a null on a REQUIRED field still fails.
    from agents.ollama_errors import PlannerValidationError
    with pytest.raises(PlannerValidationError):
        TaskPlanner._validate_plan({"goal": "visual", "steps": [
            {"tool": "visual_click", "target": None},
        ]})


def test_visual_permission_levels():
    from core.permission import PermissionEngine, PermissionLevel
    engine = PermissionEngine()
    for tool in ("screenshot", "inspect_screen", "locate_target",
                 "visual_verify"):
        level, confirm = engine.evaluate(tool, {})
        assert level == PermissionLevel.HARMLESS
        assert confirm is False
    for tool in ("visual_click", "visual_type", "visual_drag",
                 "visual_scroll"):
        level, confirm = engine.evaluate(tool, {"target": "x"})
        assert level == PermissionLevel.NORMAL
        assert confirm is False


def test_registry_lists_visual_tools():
    from backend.tools import build_default_tool_registry
    registry = build_default_tool_registry()
    expected = {"screenshot": True, "inspect_screen": True,
                "locate_target": True, "visual_click": False,
                "visual_type": False, "visual_drag": False,
                "visual_scroll": False, "visual_verify": True}
    for name, idempotent in expected.items():
        definition = registry.get(name)
        assert definition is not None, name
        assert definition.idempotent is idempotent, name


def test_visual_lessons_record_structured_memory():
    agent = make_agent()
    saved = []
    memory = type("M", (), {"save_experience": lambda self, **k: saved.append(k)})()
    agent.record_lesson(memory, "click Save",
                        {"ok": True, "verified": True, "retries": 0})
    assert saved and saved[0]["outcome"] == "success"
    assert saved[0]["scenario"].startswith("visual:")
    agent.record_lesson(memory, "click Save",
                        {"ok": False, "reason": "low_confidence"})
    assert saved[-1]["outcome"] == "failure"
    # Memory without the interface is tolerated.
    agent.record_lesson(object(), "click Save", {"ok": True})


def test_server_accepts_visual_phases():
    from backend import server
    for phase in ("VISUAL_OBSERVE", "VISUAL_LOCATE", "VISUAL_ACT",
                  "VISUAL_VERIFY", "VISUAL_RECOVER"):
        assert phase in server.VALID_PHASES
    mapping = {"screenshot", "inspect_screen", "locate_target",
               "visual_click", "visual_type", "visual_drag",
               "visual_scroll", "visual_verify",
               # Milestone 4 window/workflow tools share the HUD map.
               "list_windows", "switch_app", "window_manage",
               "extract_window_text", "read_clipboard", "run_workflow"}
    assert set(server._VISUAL_TOOL_PHASES) == mapping
