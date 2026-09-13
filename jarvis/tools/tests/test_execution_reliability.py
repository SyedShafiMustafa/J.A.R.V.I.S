from backend.interfaces import ToolResult
from tools.desktop_control import DesktopController
from tools.executor import TaskExecutor


class FakeDesktop:
    def open_app(self, app): return True
    def close_app(self, app): return True
    def open_youtube(self): return True
    def search_youtube(self, query): return True
    def search_google(self, query): return True


class FakeComputer:
    def wait_for_window(self, title): return True
    def focus_window(self, title): return True
    def type_text(self, text): pass
    def press(self, key): pass
    def hotkey(self, *keys): pass


class FakeVision:
    def click_text(self, text): return True


def executor():
    instance = TaskExecutor.__new__(TaskExecutor)
    instance.desktop = FakeDesktop()
    instance.computer = FakeComputer()
    instance.vision = FakeVision()
    return instance


def test_all_existing_tools_return_structured_success():
    result = executor().execute({
        "goal": "exercise catalog",
        "steps": [
            {"tool": "open_app", "app": "notepad"},
            {"tool": "wait_window", "title": "Notepad"},
            {"tool": "close_app", "app": "notepad"},
            {"tool": "open_youtube"},
            {"tool": "search_youtube", "query": "AI"},
            {"tool": "search_google", "query": "AI"},
            {"tool": "type", "text": "hello"},
            {"tool": "press", "key": "enter"},
            {"tool": "hotkey", "keys": ["ctrl", "s"]},
            {"tool": "click_text", "text": "Search"},
        ],
    })
    assert isinstance(result, ToolResult)
    assert result.success
    assert result.data["completed"]


def test_executor_stops_after_failed_tool():
    class FailingDesktop(FakeDesktop):
        def open_app(self, app): return False

    instance = executor()
    instance.desktop = FailingDesktop()
    result = instance.execute({
        "goal": "stop on failure",
        "steps": [
            {"tool": "open_app", "app": "missing"},
            {"tool": "type", "text": "must not run"},
        ],
    })
    assert not result.success
    assert result.tool == "open_app"
    assert not result.data["completed"]


def test_controller_exception_is_structured_without_traceback():
    class BrokenComputer(FakeComputer):
        def press(self, key): raise RuntimeError("secret traceback detail")

    instance = executor()
    instance.computer = BrokenComputer()
    result = instance.execute({"goal": "press", "steps": [{"tool": "press", "key": "enter"}]})
    assert not result.success
    assert "secret traceback" not in result.message
    assert result.data["reason"] == "controller failure"


def test_invalid_keyboard_inputs_fail_before_controller():
    instance = executor()
    result = instance.execute({"goal": "invalid", "steps": [{"tool": "press", "key": "not-a-key"}]})
    assert not result.success
    assert result.data["started"] is False


def test_close_app_rejects_empty_and_broad_targets(monkeypatch):
    controller = DesktopController.__new__(DesktopController)
    calls = []
    monkeypatch.setattr("tools.desktop_control.subprocess.run", lambda *args, **kwargs: calls.append(args) or None)
    assert controller.close_app("") is False
    assert controller.close_app("a") is False
    assert calls == []
