"""Global action-vs-explanation regression matrix.

JARVIS is an operating agent: an imperative request naming an
executable capability must EXECUTE (router fast path or planner), and
only an explicit how-to question may be answered with instructions.

For every ACTION form the decision must be executable (kind "reply"
via a real fast-path launch, or kind "action" for the planner) — never
chat. For every EXPLANATION form the decision must be "chat" (brain
explains) — never a tool execution. Desktop/focus calls are faked.
"""

import pytest

from core.intent import classify, strip_politeness, is_explanation_request

ACTION_CASES = [
    # windows
    "Maximize Chrome.",
    "Minimize Chrome.",
    "Restore Chrome.",
    "Put Chrome on the left.",
    "Put Notepad on the right.",
    "Switch to Chrome.",
    "Max Chrome.",
    "Make Chrome full-size.",
    "Bring Chrome up and maximize it.",
    # applications
    "Close Chrome.",
    # visual
    "Open the File menu.",
    "Close the File menu.",
    "Click Save.",
    # filesystem
    "Create a file called test.txt.",
    "Move this file to Downloads.",
    # terminal / dev / git / browser
    "Search Google for Python.",
    "Run pytest.",
    "Show git status.",
    # scheduler / cross-app / messaging
    "Remind me in 1 minute to test JARVIS.",
    "Copy this text and paste it into Notepad.",
    "Send this message on WhatsApp.",
    "Open Chrome, search for Python, copy the title and paste it into Notepad.",
    # polite imperatives still act
    "Please open Chrome.",
    "Can you open Chrome?",
    "Could you move this file?",
    "Can you maximize Chrome for me?",
    "Please send this message.",
    # repeat still acts (planner resolves via lessons, never explains)
    "Do that again.",
    # capabilities without tools act, then fail honestly (never explain)
    "Turn on dark mode.",
    "Install Git.",
]

EXPLAIN_CASES = [
    "How do I maximize Chrome?",
    "How can I move a file to Downloads?",
    "How do I open the File menu?",
    "How do I run pytest?",
    "How do I send a WhatsApp message?",
    "How do reminders work?",
    "How do I put Chrome on the left?",
    "How do I turn on dark mode?",
    "How do I install Git?",
    "Can Chrome be maximized?",
    "What happens if I delete this file?",
    "Why won't Chrome open?",
    "Show me how to open the File menu.",
    "Teach me how reminders work.",
]


@pytest.mark.parametrize("text", ACTION_CASES)
def test_action_forms_classify_act(text):
    assert classify(text) == "act", f"action misread: {text!r}"


@pytest.mark.parametrize("text", EXPLAIN_CASES)
def test_explanation_forms_classify_explain(text):
    assert classify(text) == "explain", f"how-to misread: {text!r}"


def test_politeness_stripping_keeps_imperative():
    assert strip_politeness("Please open Chrome.") == "open chrome."
    assert strip_politeness("Can you open Chrome?") == "open chrome?"
    assert strip_politeness("Could you move this file?") == "move this file?"
    assert is_explanation_request("How do I open Chrome?") is True
    assert is_explanation_request("Can you open Chrome?") is False
    assert is_explanation_request("Maximize Chrome?") is False


def _orchestrator(monkeypatch):
    from backend.live_adapters import LiveOrchestrator
    import core.router as router_mod

    class _Desktop:
        def __init__(self):
            self.opened = []
            self.closed = []

        def open_app(self, app):
            self.opened.append(app)
            return True

        def close_app(self, app):
            self.closed.append(app)
            return True

    class _Computer:
        def wait_for_window(self, title, timeout=8.0):
            return True

    class _Mem:
        def retrieve_experiences(self, *a, **k):
            return []

        def search_memories(self, *a, **k):
            return []

    desktop = _Desktop()
    monkeypatch.setattr(router_mod, "desktop", desktop)
    monkeypatch.setattr(router_mod, "computer", _Computer())
    orch = LiveOrchestrator.__new__(LiveOrchestrator)
    from core.router import CommandRouter
    orch.router = CommandRouter()
    orch.memory = _Mem()
    return orch, desktop


@pytest.mark.parametrize("text", [t for t in ACTION_CASES
                                  if t not in ("Open Chrome.",
                                               "Close Chrome.")])
def test_action_forms_reach_execution(monkeypatch, text):
    """Every action form must route to router-execution or the planner."""
    orch, _ = _orchestrator(monkeypatch)
    decision = orch.decide(text)
    assert decision.kind in ("reply", "action"), \
        f"{text!r} fell through to chat instead of executing"


def test_open_close_execute_through_fast_path(monkeypatch):
    orch, desktop = _orchestrator(monkeypatch)
    decision = orch.decide("Open Chrome.")
    assert decision.kind == "reply"
    assert desktop.opened == ["chrome"]
    assert "now open" in (decision.reply or "").lower()


def test_close_executes_through_fast_path(monkeypatch):
    orch, desktop = _orchestrator(monkeypatch)
    decision = orch.decide("Close Chrome.")
    assert decision.kind == "reply"
    assert desktop.closed == ["chrome"]


@pytest.mark.parametrize("text", EXPLAIN_CASES)
def test_explanation_forms_never_execute(monkeypatch, text):
    orch, desktop = _orchestrator(monkeypatch)
    decision = orch.decide(text)
    assert decision.kind == "chat", \
        f"{text!r} would execute instead of explaining"
    assert desktop.opened == [] and desktop.closed == []
