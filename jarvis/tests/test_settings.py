"""§29 hybrid Windows settings control — unit/integration/regression tests.

Covers the spec §13 matrix with fake native backends (no real registry
or audio touched): parsing, value validation, native/fallback method
selection, permission tiers, result schema, verification, failure
handling and action-vs-explanation routing.
"""

import pytest

from agents.ollama_errors import PlannerValidationError
from agents.planner import TaskPlanner
from backend.tools import build_default_tool_registry
from core.intent import classify
from core.permission import PermissionEngine, PermissionLevel
from core.router import CommandRouter
from tools.executor import TaskExecutor
from tools.settings import (
    METHOD_FAILED,
    METHOD_NATIVE,
    METHOD_UI,
    SETTINGS_REGISTRY,
    SUPPORTED_SETTINGS,
    SettingsController,
    SettingResult,
    parse_settings_command,
)


# ---------------------------------------------------------------------------
# Fakes (independent from other suites)
# ---------------------------------------------------------------------------

class _FakeDark:
    """In-memory Personalize stand-in: apps_light/system_light bools."""

    def __init__(self, apps_light=True, system_light=True,
                 write_ok=True):
        self.apps_light = apps_light
        self.system_light = system_light
        self.write_ok = write_ok
        self.writes = []

    def available(self):
        return True

    def read(self):
        dark = not self.apps_light and not self.system_light
        return {"ok": True, "apps_light": self.apps_light,
                "system_light": self.system_light, "dark": dark,
                "mixed": self.apps_light != self.system_light}

    def write(self, dark):
        self.writes.append(dark)
        if not self.write_ok:
            return {"ok": False, "reason": "native_write_failed"}
        self.apps_light = not dark
        self.system_light = not dark
        return {"ok": True}


class _FakeVolume:
    """In-memory master-volume stand-in: percent + muted."""

    def __init__(self, percent=30, muted=False, write_ok=True,
                 mute_ok=True):
        self.percent = percent
        self.muted = muted
        self.write_ok = write_ok
        self.mute_ok = mute_ok
        self.writes = []

    def available(self):
        return True

    def read(self):
        return {"ok": True, "percent": self.percent,
                "muted": self.muted}

    def write_percent(self, percent):
        self.writes.append(("percent", percent))
        if not self.write_ok:
            return {"ok": False, "reason": "native_write_failed"}
        self.percent = max(0, min(100, int(percent)))
        return {"ok": True, "requested": self.percent}

    def write_mute(self, muted):
        self.writes.append(("mute", muted))
        if not self.mute_ok:
            return {"ok": False, "reason": "mute_control_missing"}
        self.muted = bool(muted)
        return {"ok": True, "requested_muted": self.muted}


def _controller(dark=None, volume=None, ui_fallback=None):
    return SettingsController(
        dark=dark or _FakeDark(), volume=volume or _FakeVolume(),
        ui_fallback=ui_fallback)


# ---------------------------------------------------------------------------
# Parsing (§8): action forms execute, how-to forms do not
# ---------------------------------------------------------------------------

_PARSE_ACTION = [
    ("Turn on dark mode.", "set_setting",
     {"setting": "dark_mode", "value": "on"}),
    ("Turn off dark mode.", "set_setting",
     {"setting": "dark_mode", "value": "off"}),
    ("Make Windows dark.", "set_setting",
     {"setting": "dark_mode", "value": "on"}),
    ("Switch to light mode.", "set_setting",
     {"setting": "dark_mode", "value": "off"}),
    ("Switch to dark mode.", "set_setting",
     {"setting": "dark_mode", "value": "on"}),
    ("Set my volume to 50%.", "set_setting",
     {"setting": "system_volume", "value": 50}),
    ("Set volume to 75.", "set_setting",
     {"setting": "system_volume", "value": 75}),
    ("Mute the volume.", "set_setting",
     {"setting": "system_volume", "value": "mute"}),
    ("Unmute.", "set_setting",
     {"setting": "system_volume", "value": "unmute"}),
    ("What's my volume?", "get_setting",
     {"setting": "system_volume"}),
    ("Is dark mode enabled?", "get_setting",
     {"setting": "dark_mode"}),
    ("Can you turn on dark mode?", "set_setting",
     {"setting": "dark_mode", "value": "on"}),
    ("Please set the volume to 50%.", "set_setting",
     {"setting": "system_volume", "value": 50}),
]

_PARSE_NONE = [
    "How do I turn on dark mode?",
    "How can I change my volume?",
    "How do I change volume?",
    "Tell me about dark matter.",
    "What time is it?",
    "",
]


@pytest.mark.parametrize("text,tool,payload", _PARSE_ACTION)
def test_parse_action_forms(text, tool, payload):
    assert parse_settings_command(text) == (tool, payload)


@pytest.mark.parametrize("text", _PARSE_NONE)
def test_parse_non_settings_returns_none(text):
    assert parse_settings_command(text) is None


# ---------------------------------------------------------------------------
# Intent: act for commands, explain for how-to
# ---------------------------------------------------------------------------

_INTENT_ACT = [
    "Turn on dark mode.",
    "Turn off dark mode.",
    "Make Windows dark.",
    "Switch to light mode.",
    "Set my volume to 50%.",
    "Set volume to 75.",
    "Mute the volume.",
    "Unmute.",
    "What's my volume?",
    "What is my current volume?",
    "Is dark mode enabled?",
    "Is dark mode on?",
]

_INTENT_EXPLAIN = [
    "How do I turn on dark mode?",
    "How can I change my volume?",
    "How do I change volume?",
]


@pytest.mark.parametrize("text", _INTENT_ACT)
def test_settings_commands_classify_act(text):
    assert classify(text) == "act", f"settings command misread: {text!r}"


@pytest.mark.parametrize("text", _INTENT_EXPLAIN)
def test_settings_howto_classifies_explain(text):
    assert classify(text) == "explain", f"how-to misread: {text!r}"


# ---------------------------------------------------------------------------
# Router: settings defer to the planner, never launch an app
# ---------------------------------------------------------------------------

class _Desktop:
    def __init__(self):
        self.opened = []

    def open_app(self, app):
        self.opened.append(app)
        return True

    def close_app(self, app):
        return False


class _Computer:
    def wait_for_window(self, title, timeout=8.0):
        return True


@pytest.mark.parametrize("text", _INTENT_ACT)
def test_router_never_launches_for_settings(monkeypatch, text):
    import core.router as router_mod
    desktop = _Desktop()
    monkeypatch.setattr(router_mod, "desktop", desktop)
    monkeypatch.setattr(router_mod, "computer", _Computer())
    handled, _ = CommandRouter().route(text)
    assert desktop.opened == [], f"{text!r} launched an app"
    assert handled is False, f"{text!r} must defer to the planner"


# ---------------------------------------------------------------------------
# READ → CHANGE → VERIFY: dark mode (native path)
# ---------------------------------------------------------------------------

def test_dark_read_reports_state():
    res = _controller(dark=_FakeDark(apps_light=False,
                                     system_light=False)).get_setting(
        "dark_mode")
    assert res.success and res.verified and res.method == METHOD_NATIVE
    assert res.new_value == "on"
    assert "on" in res.message.lower()


def test_dark_change_verifies_new_state():
    ctl = _controller(dark=_FakeDark(apps_light=True,
                                     system_light=True))
    res = ctl.set_setting("dark_mode", "on")
    assert res.success and res.verified and res.method == METHOD_NATIVE
    assert res.previous_value == "off"
    assert res.new_value == "on"
    assert res.evidence["fallback_used"] is False
    assert "total_ms" in res.evidence


def test_dark_already_there_reports_without_write():
    fake = _FakeDark(apps_light=False, system_light=False)
    res = _controller(dark=fake).set_setting("dark_mode", "on")
    assert res.success and res.verified
    assert fake.writes == []
    assert "already" in res.message.lower()


def test_dark_mixed_state_normalizes_and_verifies():
    ctl = _controller(dark=_FakeDark(apps_light=True,
                                     system_light=False))
    res = ctl.set_setting("dark_mode", "off")
    assert res.success and res.verified
    assert res.previous_value == "mixed"


# ---------------------------------------------------------------------------
# READ → CHANGE → VERIFY: volume (native path)
# ---------------------------------------------------------------------------

def test_volume_read_reports_level():
    res = _controller(volume=_FakeVolume(percent=42)).get_setting(
        "system_volume")
    assert res.success and res.verified
    assert res.evidence["percent"] == 42
    assert "42%" in res.message


def test_volume_change_verifies_level():
    ctl = _controller(volume=_FakeVolume(percent=20))
    res = ctl.set_setting("system_volume", 50)
    assert res.success and res.verified and res.method == METHOD_NATIVE
    assert res.previous_value == "20%"
    assert res.new_value == "50%"
    assert res.evidence["target"] == 50


def test_volume_mute_unmute_verify():
    ctl = _controller(volume=_FakeVolume(percent=40, muted=False))
    muted = ctl.set_setting("system_volume", "mute")
    assert muted.success and muted.verified
    assert muted.new_value == "40% (muted)"
    unmuted = ctl.set_setting("system_volume", "unmute")
    assert unmuted.success and unmuted.verified
    assert unmuted.new_value == "40%"


def test_volume_up_down_step():
    ctl = _controller(volume=_FakeVolume(percent=50))
    up = ctl.set_setting("system_volume", "up")
    assert up.success and up.new_value == "60%"
    down = ctl.set_setting("system_volume", "down")
    assert down.success and down.new_value == "50%"


def test_volume_out_of_range_rejected_without_acting():
    fake = _FakeVolume(percent=30)
    res = _controller(volume=fake).set_setting("system_volume", 150)
    assert not res.success and not res.verified
    assert res.error == "invalid_value"
    assert fake.writes == []


# ---------------------------------------------------------------------------
# Fallback selection (§6/§7): native fail → UI fallback → verify / honest fail
# ---------------------------------------------------------------------------

def test_native_failure_falls_back_and_verifies():
    fake = _FakeDark(apps_light=True, system_light=True,
                     write_ok=False)

    def _fallback(setting, value):
        fake.apps_light = not value
        fake.system_light = not value
        return {"ok": True, "reason": "settings_page_click"}

    res = _controller(dark=fake, ui_fallback=_fallback).set_setting(
        "dark_mode", "on")
    assert res.success and res.verified
    assert res.method == METHOD_UI
    assert res.evidence["fallback_used"] is True
    assert "native_error" in res.evidence


def test_refused_fallback_fails_honestly():
    fake = _FakeDark(apps_light=True, system_light=True,
                     write_ok=False)

    def _fallback(setting, value):
        return {"ok": False, "reason": "low_confidence"}

    res = _controller(dark=fake, ui_fallback=_fallback).set_setting(
        "dark_mode", "on")
    assert not res.success and not res.verified
    assert res.method == METHOD_FAILED
    assert res.error == "verification_unavailable"


def test_fallback_disabled_fails_honestly():
    fake = _FakeDark(apps_light=True, system_light=True,
                     write_ok=False)
    res = _controller(dark=fake).set_setting("dark_mode", "on",
                                             fallback_allowed=False)
    assert not res.success and not res.verified
    assert res.method == METHOD_FAILED
    assert res.evidence["fallback_used"] is False


def test_verification_mismatch_is_not_success():
    class _LyingDark(_FakeDark):
        def write(self, dark):
            self.writes.append(dark)
            return {"ok": True}  # claims write, state unchanged

    res = _controller(dark=_LyingDark(apps_light=False,
                                      system_light=False)).set_setting(
        "dark_mode", "off", fallback_allowed=False)
    assert not res.success and not res.verified
    assert "verify_mismatch" in res.evidence


def test_unreadable_state_reports_verification_unavailable():
    class _BlindDark(_FakeDark):
        def read(self):
            return {"ok": False, "reason": "native_unavailable"}

    res = _controller(dark=_BlindDark()).get_setting("dark_mode")
    assert not res.success and not res.verified
    assert res.error == "verification_unavailable"


def test_unsupported_setting_rejected():
    res = _controller().set_setting("airplane_mode", "on")
    assert not res.success
    assert res.error == "unsupported_setting"
    assert res.retryable is False
    read = _controller().get_setting("airplane_mode")
    assert not read.success
    assert read.error == "unsupported_setting"


# ---------------------------------------------------------------------------
# Result schema (§2)
# ---------------------------------------------------------------------------

def test_result_schema_keys():
    res = _controller().get_setting("dark_mode")
    assert set(res.to_dict()) == {
        "success", "verified", "method", "previous_value",
        "new_value", "message", "evidence", "retryable", "error"}
    assert isinstance(res, SettingResult)


# ---------------------------------------------------------------------------
# Permission integration (§5): existing tiers only
# ---------------------------------------------------------------------------

def test_settings_permission_tiers():
    engine = PermissionEngine()
    level, confirm = engine.evaluate("get_setting",
                                     {"setting": "dark_mode"})
    assert level == PermissionLevel.HARMLESS
    assert confirm is False
    level, confirm = engine.evaluate("set_setting",
                                     {"setting": "dark_mode",
                                      "value": "on"})
    assert level == PermissionLevel.NORMAL
    assert confirm is False
    # No new tier exists anywhere in the vocabulary.
    assert {level.value for level in PermissionLevel} == {
        "harmless", "normal", "sensitive", "destructive"}


# ---------------------------------------------------------------------------
# Planner contract: schemas accept pilot tools, reject bad payloads
# ---------------------------------------------------------------------------

_PLANNER_ACCEPT = [
    {"goal": "g", "steps": [{"tool": "get_setting",
                             "setting": "dark_mode"}]},
    {"goal": "g", "steps": [{"tool": "get_setting",
                             "setting": "system_volume"}]},
    {"goal": "g", "steps": [{"tool": "set_setting",
                             "setting": "dark_mode",
                             "value": "on"}]},
    {"goal": "g", "steps": [{"tool": "set_setting",
                             "setting": "dark_mode",
                             "value": "off"}]},
    {"goal": "g", "steps": [{"tool": "set_setting",
                             "setting": "system_volume",
                             "value": 50}]},
    {"goal": "g", "steps": [{"tool": "set_setting",
                             "setting": "system_volume",
                             "value": "mute"}]},
    {"goal": "g", "steps": [{"tool": "set_setting",
                             "setting": "system_volume",
                             "value": "up"}]},
]

_PLANNER_REJECT = [
    {"goal": "g", "steps": [{"tool": "get_setting"}]},
    {"goal": "g", "steps": [{"tool": "get_setting",
                             "setting": "brightness"}]},
    {"goal": "g", "steps": [{"tool": "set_setting",
                             "setting": "dark_mode"}]},
    {"goal": "g", "steps": [{"tool": "set_setting",
                             "setting": "dark_mode",
                             "value": "maybe"}]},
    {"goal": "g", "steps": [{"tool": "set_setting",
                             "setting": "system_volume",
                             "value": 150}]},
    {"goal": "g", "steps": [{"tool": "set_setting",
                             "setting": "system_volume",
                             "value": True}]},
    {"goal": "g", "steps": [{"tool": "set_setting",
                             "setting": "airplane_mode",
                             "value": "on"}]},
]


@pytest.mark.parametrize("plan", _PLANNER_ACCEPT)
def test_planner_accepts_settings_tools(plan):
    TaskPlanner._validate_plan(plan)


@pytest.mark.parametrize("plan", _PLANNER_REJECT)
def test_planner_rejects_bad_settings_payloads(plan):
    with pytest.raises(PlannerValidationError):
        TaskPlanner._validate_plan(plan)


# ---------------------------------------------------------------------------
# Executor integration: tools dispatch through the shared controller
# ---------------------------------------------------------------------------

def test_executor_get_setting_end_to_end(monkeypatch):
    from tools import executor as executor_mod
    ctl = _controller(dark=_FakeDark(apps_light=False,
                                     system_light=False))
    monkeypatch.setattr(executor_mod.TaskExecutor, "__init__",
                        lambda self: setattr(self, "settings", ctl))
    ex = executor_mod.TaskExecutor()
    res = ex._execute_step("get_setting", {"tool": "get_setting",
                                           "setting": "dark_mode"})
    assert res.success and res.data["verified"] is True
    assert res.data["method"] == METHOD_NATIVE


def test_executor_set_volume_end_to_end(monkeypatch):
    from tools import executor as executor_mod
    ctl = _controller(volume=_FakeVolume(percent=20))
    monkeypatch.setattr(executor_mod.TaskExecutor, "__init__",
                        lambda self: setattr(self, "settings", ctl))
    ex = executor_mod.TaskExecutor()
    res = ex._execute_step("set_setting",
                           {"tool": "set_setting",
                            "setting": "system_volume", "value": 50})
    assert res.success and res.data["verified"] is True
    assert res.data["new_value"] == "50%"


def test_executor_rejects_bad_settings_steps():
    ex = TaskExecutor.__new__(TaskExecutor)
    bad = ex._validate_step("set_setting",
                            {"tool": "set_setting",
                             "setting": "dark_mode", "value": "maybe"})
    assert bad is not None and not bad.success
    bad = ex._validate_step("get_setting",
                            {"tool": "get_setting",
                             "setting": "brightness"})
    assert bad is not None and not bad.success
    good = ex._validate_step("set_setting",
                             {"tool": "set_setting",
                              "setting": "system_volume", "value": 50})
    assert good is None


# ---------------------------------------------------------------------------
# Registry / capability model (§12)
# ---------------------------------------------------------------------------

def test_registry_lists_settings_tools():
    registry = build_default_tool_registry()
    assert registry.get("get_setting") is not None
    assert registry.get("set_setting") is not None
    assert registry.get("get_setting").idempotent is True
    assert registry.get("set_setting").idempotent is False
    assert registry.validate_payload(
        "set_setting", {"setting": "dark_mode",
                        "value": "on"}) is None


def test_settings_registry_capabilities():
    assert set(SUPPORTED_SETTINGS) == {"dark_mode", "system_volume"}
    for name, entry in SETTINGS_REGISTRY.items():
        for key in ("read", "write", "native", "fallback",
                    "verification", "permission_read",
                    "permission_write", "values", "limitations"):
            assert key in entry, f"{name} missing {key}"
    assert SETTINGS_REGISTRY["dark_mode"]["permission_read"] == \
        "harmless"
    assert SETTINGS_REGISTRY["dark_mode"]["permission_write"] == \
        "normal"


# ---------------------------------------------------------------------------
# Server phases (§17): settings tools surface without breaking the HUD map
# ---------------------------------------------------------------------------

def test_server_accepts_settings_phases():
    from backend import server as server_mod
    assert server_mod._VISUAL_TOOL_PHASES["get_setting"] == \
        server_mod.PHASE_SETTINGS_READ
    assert server_mod._VISUAL_TOOL_PHASES["set_setting"] == \
        server_mod.PHASE_SETTINGS_CHANGE
    assert server_mod.PHASE_SETTINGS_READ in server_mod.VALID_PHASES
    assert server_mod.PHASE_SETTINGS_CHANGE in server_mod.VALID_PHASES
    assert server_mod._VISUAL_STAGE_LABELS["get_setting"] == \
        "SETTINGS READ"
    assert server_mod._VISUAL_STAGE_LABELS["set_setting"] == \
        "SETTINGS CHANGE"


# ---------------------------------------------------------------------------
# Experience lessons (§11): structured, no screenshots
# ---------------------------------------------------------------------------

def test_lessons_record_method_and_outcome():
    ctl = _controller(dark=_FakeDark(apps_light=True,
                                     system_light=True))
    lesson = SettingsController.build_lesson(
        ctl.set_setting("dark_mode", "on"))
    assert lesson["outcome"] == "success"
    assert "native" in lesson["lesson"]
    assert "screenshot" not in str(lesson).lower()


def test_lessons_record_fallback_quirk():
    fake = _FakeDark(apps_light=True, system_light=True,
                     write_ok=False)

    def _fallback(setting, value):
        fake.apps_light = not value
        fake.system_light = not value
        return {"ok": True, "reason": "settings_page_click"}

    lesson = SettingsController.build_lesson(
        _controller(dark=fake,
                    ui_fallback=_fallback).set_setting("dark_mode",
                                                       "on"))
    assert lesson["outcome"] == "success"
    assert "fallback" in lesson["lesson"]
