"""Routing / intent-precedence regression tests.

Headline bug: "open the File menu" must never launch (or pretend to
launch) an application — it addresses on-screen UI and belongs to the
planner's visual tools. Same for every ambiguous open/close/file/menu
shape below. Desktop and focus calls are faked: no real app launches.
"""

import pytest

from core.router import CommandRouter


class _Desktop:
    def __init__(self):
        self.opened = []
        self.closed = []

    def open_app(self, app):
        self.opened.append(app)
        return app in ("notepad", "file explorer", "chrome")

    def close_app(self, app):
        self.closed.append(app)
        return app == "chrome"


class _Computer:
    def __init__(self):
        self.waited = []

    def wait_for_window(self, title, timeout=8.0):
        self.waited.append(title)
        return True


@pytest.fixture()
def router(monkeypatch):
    import core.router as router_mod
    desktop = _Desktop()
    computer = _Computer()
    monkeypatch.setattr(router_mod, "desktop", desktop)
    monkeypatch.setattr(router_mod, "computer", computer)
    return CommandRouter(), desktop, computer


def test_file_menu_never_launches(router):
    r, desktop, _ = router
    for text in ("open the File menu",
                 "open File menu",
                 "close the File menu",
                 "click the File menu",
                 "open the Settings menu"):
        handled, reply = r.route(text)
        assert handled is False, f"menu intent launched: {text!r}"
    assert desktop.opened == [] and desktop.closed == []


def test_plain_open_still_fast_pathed(router):
    r, desktop, computer = router
    handled, reply = r.route("open notepad")
    assert handled is True
    assert "notepad" in reply.lower()
    assert desktop.opened == ["notepad"]
    assert computer.waited == ["notepad"]


def test_file_explorer_open_still_fast_pathed(router):
    r, desktop, _ = router
    handled, reply = r.route("open File Explorer")
    assert handled is True
    assert desktop.opened == ["file explorer"]


def test_file_document_intent_defers_to_planner(router):
    r, desktop, _ = router
    for text in ("open this file",
                 "open report.pdf",
                 "start a new file",
                 "open my downloads folder"):
        handled, _ = r.route(text)
        assert handled is False, f"file intent launched: {text!r}"
    assert desktop.opened == []


def test_run_use_verbs_do_not_launch(router):
    r, desktop, _ = router
    for text in ("run a workflow",
                 "use Chrome to search for cats",
                 "run git status"):
        handled, _ = r.route(text)
        assert handled is False, f"verb misrouted to launch: {text!r}"
    assert desktop.opened == []


def test_substring_chat_never_routes(router):
    r, desktop, _ = router
    for text in ("Thailand is nice",
                 "I understand the assignment",
                 "please disclose the report",
                 "I already read that",
                 "what time is it"):
        handled, _ = r.route(text)
        assert handled is False, f"chat misrouted: {text!r}"
    assert desktop.opened == [] and desktop.closed == []


def test_unknown_app_fails_truthfully(router):
    r, desktop, _ = router
    handled, reply = r.route("open weather today")
    assert handled is True
    assert "couldn't find" in reply and "weather today" in reply
    assert desktop.opened == ["weather today"]


def test_close_reports_truthfully(router):
    r, desktop, _ = router
    handled, reply = r.route("close chrome")
    assert handled is True
    assert reply == "Closed chrome."
    handled, reply = r.route("close notepad")
    assert handled is True
    assert "couldn't find" in reply


def test_missing_app_suggests_menu_path(router):
    r, _, _ = router
    handled, reply = r.route("open file menu pro")
    # "menu" + file hints -> planner, not a launch attempt.
    assert handled is False


def test_action_request_token_matching():
    from backend.live_adapters import _is_action_request
    assert _is_action_request("open notepad") is True
    assert _is_action_request("run git status") is True
    assert _is_action_request("send an email to bob") is True
    assert _is_action_request("I already read that") is False
    assert _is_action_request("listen to music") is False
    assert _is_action_request("latest news please") is False
    assert _is_action_request("my profile picture") is False
    assert _is_action_request("Thailand is nice") is False
    assert _is_action_request("what time is it") is False
