"""
tests/test_whatsapp_and_permission.py

Automated unit tests for WhatsApp messaging flow, parsing, verification,
and the permission engine / confirmation lifecycle.
"""

import tempfile
from pathlib import Path
import pytest

from tools.whatsapp import (
    parse_whatsapp_request,
    WhatsAppManager,
    name_match,
    resolve_clarification_reply,
)
from tools.vision import clean_sidebar_name
from core.permission import PermissionEngine, ConfirmationStore, PermissionLevel, ActionStatus
from backend.interfaces import ToolResult


# ── WhatsApp Parsing Tests ──────────────────────────────────────────────────

def test_parse_whatsapp_request_quoted():
    res = parse_whatsapp_request("Send Ahmed 'I'm reaching in 10 minutes' on WhatsApp")
    assert res is not None
    assert res["recipient"] == "Ahmed"
    assert res["message"] == "I'm reaching in 10 minutes"


def test_parse_whatsapp_request_unquoted():
    res = parse_whatsapp_request("Send Mom Happy Birthday on WhatsApp")
    assert res is not None
    assert res["recipient"] == "Mom"
    assert res["message"] == "Happy Birthday"


def test_parse_whatsapp_request_invalid():
    res = parse_whatsapp_request("Open Chrome and search for news")
    assert res is None


@pytest.mark.parametrize(
    "text,recipient,message",
    [
        # The phrasings from real voice logs that previously fell through to
        # the planner and produced garbage recipients.
        (
            "Can you send a message on whatsapp to Afan vr saying this is a test message?",
            "Afan vr",
            "this is a test message",
        ),
        ("Can you send Mama a message on WhatsApp saying hello?", "Mama", "hello"),
        ("Can you open WhatsApp and text mamma hai?", "mamma", "hai"),
        (
            "open WhatsApp and text Affan Bhaiyya a hello message",
            "Affan Bhaiyya",
            "a hello message",
        ),
        (
            "open whatsapp and send Affan Bhaiyya this is a test message",
            "Affan Bhaiyya",
            "this is a test message",
        ),
        ("Message Ahmed Hello on WhatsApp", "Ahmed", "Hello"),
        ("send a message to Mom saying hi on whatsapp", "Mom", "hi"),
    ],
)
def test_parse_whatsapp_request_natural_phrasings(text, recipient, message):
    assert parse_whatsapp_request(text) == {"recipient": recipient, "message": message}


def test_parse_whatsapp_requires_whatsapp_mention():
    # A generic "send" request must not be hijacked into WhatsApp automation.
    assert parse_whatsapp_request("send a message to Mom saying hi") is None
    assert parse_whatsapp_request("what time is it") is None


# ── WhatsApp Automation Flow Tests ──────────────────────────────────────────

def test_whatsapp_manager_send_success(monkeypatch):
    class FakeDesktop:
        def open_app(self, app):
            return True

    class FakeComputer:
        def focus_window(self, title):
            return True
        def hotkey(self, *keys):
            pass
        def press(self, key):
            pass
        def type_text(self, text):
            pass
        def set_clipboard(self, text):
            pass
        def paste(self):
            pass
        def get_active_window(self):
            return "WhatsApp"

    class FakeVision:
        def click_text(self, target):
            return True

        def read_header(self):
            return "WhatsApp  Ahmed"

        def read_screen(self):
            # The sent message is now visible in the conversation.
            return {"window": "WhatsApp", "text": "Ahmed I'm reaching in 10 minutes"}

    manager = WhatsAppManager(
        desktop=FakeDesktop(),
        computer=FakeComputer(),
        vision=FakeVision(),
    )

    res = manager.send_message("Ahmed", "I'm reaching in 10 minutes")
    assert res.success is True
    assert res.tool == "send_whatsapp"
    assert res.data["verified"] is True
    assert res.data["evidence"] == "message_visible_in_conversation"
    assert res.data["recipient"] == "Ahmed"


def test_whatsapp_manager_reports_failure_when_send_not_observed():
    """Keystrokes ran but nothing confirms delivery -> honest failure, no fake success."""

    class FakeDesktop:
        def open_app(self, app):
            return True

    class FakeComputer:
        def focus_window(self, title):
            return True
        def hotkey(self, *keys):
            pass
        def press(self, key):
            pass
        def type_text(self, text):
            pass
        def set_clipboard(self, text):
            pass
        def paste(self):
            pass
        def get_active_window(self):
            return "WhatsApp"

    class FakeVision:
        def click_text(self, target):
            return True

        def read_header(self):
            return "WhatsApp  Ahmed"

        def read_screen(self):
            # Conversation is empty; the message never appeared.
            return {"window": "WhatsApp", "text": ""}

    manager = WhatsAppManager(
        desktop=FakeDesktop(),
        computer=FakeComputer(),
        vision=FakeVision(),
    )

    res = manager.send_message("Ahmed", "Hello")
    assert res.success is False
    assert res.data["verified"] is False
    assert res.data["evidence"] == "no_visual_confirmation"


# ── Conversation-identity guard ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "header,recipient,expected",
    [
        ("WhatsApp  Shafi Ahmed", "Shafi Ahmed", True),
        ("Ahmed Munawar", "Munawar Ahmed", True),  # order-insensitive
        ("WhatsApp  Vigar Ahmed Munawar", "Ahmed", False),  # ambiguous -> refuse
        ("MD OMAIR AHMED", "Ahmed", False),
        ("", "Ahmed", False),
        ("WhatsApp", "Ahmed", False),  # UI noise only
        # Saved nickname whose last token is the name the user asked for.
        ("Bhaiyya Mama", "Mama", True),
        ("Bhaiyya Mama", "mamma", True),  # STT typo tolerance
        # Single token that is NOT the header's last name stays refused.
        ("Bhaiyya Mama", "Bhaiyya", False),
        # Full name present inside a noisier header.
        (
            "Affan Bhaiyya (Affan click here for contact info",
            "Affan Bhaiyya",
            True,
        ),
    ],
)
def test_conversation_guard(header, recipient, expected):
    assert WhatsAppManager._conversation_matches(header, recipient) is expected


def test_send_aborts_before_composer_on_wrong_conversation():
    """Search is fuzzy; if the opened chat is not the intended person we must
    not type or send anything."""
    typed = []

    class FakeDesktop:
        def open_app(self, app):
            return True

    class FakeComputer:
        def focus_window(self, title):
            return True
        def hotkey(self, *keys):
            pass
        def press(self, key):
            pass
        def type_text(self, text):
            typed.append(text)
        def set_clipboard(self, text):
            typed.append(text)
        def paste(self):
            typed.append("PASTE")
        def get_active_window(self):
            return "WhatsApp"

    class FakeVision:
        def click_text(self, target):
            return True
        def read_header(self):
            return "Vigar Ahmed Munawar"  # wrong person
        def read_screen(self):
            return {"window": "WhatsApp", "text": "whatever"}

    manager = WhatsAppManager(desktop=FakeDesktop(), computer=FakeComputer(), vision=FakeVision())
    res = manager.send_message("Ahmed", "secret message")
    assert res.success is False
    assert res.data["stage"] == "conversation_match"
    # The message body must never have been typed/pasted.
    assert "secret message" not in typed


# ── Name matching + sidebar (search result) reading ────────────────────────

@pytest.mark.parametrize(
    "query,candidate,confident",
    [
        # STT spelling variants of the same contact must count as a match.
        ("mamma", "Mumma", True),
        ("mumma", "Mumma", True),
        ("afan", "Affan", True),
        ("mumma", "Mumma Family", False),  # extra word -> ask, don't guess
        ("ahmed", "Vigar Ahmed Munawar", False),  # a first name is not enough
        ("Affan Bhaiyya", "Affan Bhaiyya", True),
    ],
)
def test_name_match_tolerates_stt_typos(query, candidate, confident):
    assert name_match(query, candidate)["confident"] is confident


@pytest.mark.parametrize(
    "line,expected",
    [
        ("Mumma 1:51 pm", "Mumma"),
        ("Mumtaz Bakhatiyar", "Mumtaz Bakhatiyar"),
        ("@ YOUSUF UDDIN'S 1:23 pm", "YOUSUF UDDIN'S"),
        ("Chats", ""),
        ("Contacts", ""),
        ("Groups in common", ""),
        ("All Unread 2 Favourites", ""),
        ("Mumma is also in this group", ""),
        ("", ""),
    ],
)
def test_clean_sidebar_name(line, expected):
    assert clean_sidebar_name(line) == expected


class _FakeDesktop:
    def __init__(self, result=True):
        self.result = result
        self.opened = []

    def open_app(self, app):
        self.opened.append(app)
        return self.result


class _FakeComputer:
    def __init__(self, active="WhatsApp"):
        self.active = active
        self.typed = []
        self.clicks = []
        self.on_click = None

    def focus_window(self, title):
        return True

    def hotkey(self, *keys):
        pass

    def press(self, key):
        self.typed.append(f"KEY:{key}")

    def type_text(self, text):
        self.typed.append(text)

    def set_clipboard(self, text):
        self.typed.append(f"CLIP:{text}")

    def paste(self):
        self.typed.append("PASTE")

    def click(self, x=None, y=None):
        self.clicks.append((x, y))
        if self.on_click:
            self.on_click(x, y)

    def get_active_window(self):
        return self.active


class _FakeVision:
    def __init__(self, rows=None, header="", screen=""):
        self.rows = rows or []
        self.header = header
        self.screen = screen
        self.clicked = []

    def window_bounds(self):
        return (0, 0, 1000, 1000)

    def click_text(self, target):
        self.clicked.append(target)
        return True

    def read_header(self):
        return self.header

    def read_sidebar_rows(self):
        return list(self.rows)

    def read_screen(self):
        return {"window": "WhatsApp", "text": self.screen}


def _manager(computer, vision, desktop=None):
    return WhatsAppManager(
        desktop=desktop or _FakeDesktop(), computer=computer, vision=vision
    )


def test_ambiguous_similar_names_ask_instead_of_sending():
    """Two saved variants match the request; JARVIS must ask, not guess."""
    rows = [
        {"name": "Mumma", "x": 120, "y": 240},
        {"name": "Mama", "x": 120, "y": 300},
    ]
    # The chat WhatsApp opened for us is a different person entirely.
    vision = _FakeVision(rows=rows, header="Someone Else", screen="whatever")
    computer = _FakeComputer()

    res = _manager(computer, vision).send_message("mamma", "secret message")

    assert res.success is False
    assert res.data["needs_clarification"] is True
    assert set(res.data["candidates"]) == {"Mumma", "Mama"}
    assert res.data["message"] == "secret message"
    assert "which one" in res.message.lower()
    # nothing was sent, and the body was never typed into a chat
    assert computer.clicks == []
    assert not any("secret" in t for t in computer.typed)


def test_unequal_confidence_asks_before_switching_chat():
    """A single weak match (extra word in the name) must still ask first."""
    rows = [{"name": "Mumma Family", "x": 120, "y": 300}]
    vision = _FakeVision(rows=rows, header="Mumma Family", screen="whatever")
    computer = _FakeComputer()

    res = _manager(computer, vision).send_message("mumma", "secret message")

    assert res.success is False
    assert res.data["needs_clarification"] is True
    assert res.data["candidates"] == ["Mumma Family"]
    assert "did you mean" in res.message.lower()
    assert not any("secret" in t for t in computer.typed)


def test_single_confident_candidate_row_is_opened_and_sent():
    """The opened chat was wrong, but exactly one sidebar row matches: use it."""
    rows = [
        {"name": "Mumma", "x": 120, "y": 240},
        {"name": "Mumtaz Bakhatiyar", "x": 120, "y": 300},
    ]
    vision = _FakeVision(rows=rows, header="Mumtaz Bakhatiyar", screen="")
    computer = _FakeComputer()

    def _after_click(x, y):
        if (x, y) == (120, 240):
            vision.header = "Mumma"
            vision.screen = "Mumma hello there"

    computer.on_click = _after_click

    res = _manager(computer, vision).send_message("mamma", "hello there")

    assert computer.clicks == [(120, 240)]
    assert res.success is True
    assert res.data["recipient"] == "Mumma"
    assert res.data["evidence"] == "message_visible_in_conversation"


def test_garbled_recipient_offers_the_closest_real_chat():
    """STT mangled the name ("fan bye ya"): offer WhatsApp's real match."""
    rows = [{"name": "Affan Bhaiyya", "x": 120, "y": 240}]
    vision = _FakeVision(rows=rows, header="Someone Else", screen="")
    computer = _FakeComputer()

    res = _manager(computer, vision).send_message("fan bye ya", "hi")

    assert res.success is False
    assert res.data["needs_clarification"] is True
    assert res.data["candidates"] == ["Affan Bhaiyya"]
    assert "did you mean" in res.message.lower()


def test_search_box_text_is_not_treated_as_a_chat():
    """OCR reads the typed query in the search field as a top row; ignore it."""
    rows = [
        {"name": "Xe (a fan bye ya", "x": 138, "y": 152},  # search box echo
        {"name": "Affan Bhaiyya", "x": 300, "y": 340},
    ]
    vision = _FakeVision(rows=rows, header="", screen="")
    manager = _manager(_FakeComputer(), vision)

    kept = manager._drop_query_echo(list(rows), "fan bye ya")

    assert [r["name"] for r in kept] == ["Affan Bhaiyya"]


def test_no_match_at_all_refuses_clearly():
    vision = _FakeVision(rows=[], header="", screen="")
    computer = _FakeComputer()

    res = _manager(computer, vision).send_message("fan bye ya", "secret")

    assert res.success is False
    assert res.data["stage"] == "conversation_match"
    assert "couldn't find" in res.message.lower()
    assert not any("secret" in t for t in computer.typed)


def test_sidebar_row_extraction_from_ocr_data():
    """The OCR grouping keeps real chat names and drops sections/previews."""
    from tools.vision import ScreenVision

    data = {
        "text": ["Mumma", "1:51", "pm", "Chats", "Mumtaz", "Bakhatiyar", "low"],
        "conf": ["90", "88", "91", "95", "92", "90", "15"],
        "left": [10, 40, 70, 10, 10, 60, 5],
        "top": [100, 100, 100, 50, 200, 200, 260],
        "width": [30, 20, 18, 40, 40, 60, 20],
        "height": [12, 12, 12, 12, 12, 12, 12],
        "block_num": [1, 1, 1, 1, 2, 2, 2],
        "par_num": [1, 1, 1, 1, 1, 1, 1],
        "line_num": [2, 2, 2, 1, 1, 1, 3],
    }

    rows = ScreenVision.lines_to_rows(data, offset=(100, 200))

    assert [r["name"] for r in rows] == ["Mumma", "Mumtaz Bakhatiyar"]
    assert rows[0]["y"] == 306  # offset applied to the name line
    assert all(r["x"] >= 100 for r in rows)


def test_no_plausible_candidate_refuses_without_typing():
    vision = _FakeVision(rows=[{"name": "Mumtaz Bakhatiyar", "x": 1, "y": 2}], header="Zzz Qqq")
    computer = _FakeComputer()

    res = _manager(computer, vision).send_message("Napoleon", "secret")

    assert res.success is False
    assert res.data["stage"] == "conversation_match"
    assert not any("secret" in t for t in computer.typed)


def test_aborts_before_typing_when_whatsapp_never_activates():
    """Keystrokes must never go to whatever window happens to be in front."""
    computer = _FakeComputer(active="Opera")
    vision = _FakeVision(header="Ahmed")
    desktop = _FakeDesktop(result=True)
    manager = _manager(computer, vision, desktop=desktop)
    manager.focus_timeout = 0.2

    res = manager.send_message("Ahmed", "secret message")

    assert res.success is False
    assert res.data["stage"] == "focus"
    assert "secret message" not in "".join(computer.typed)


@pytest.mark.parametrize(
    "reply,options,expected",
    [
        ("the second one", ["Mumma", "Mumma Family"], {"choice": "Mumma Family"}),
        ("1", ["Mumma", "Mumma Family"], {"choice": "Mumma"}),
        ("Mumma Family", ["Mumma", "Mumma Family"], {"choice": "Mumma Family"}),
        ("mumma", ["Mumma", "Mumtaz Bakhatiyar"], {"choice": "Mumma"}),
        ("yes", ["Affan Bhaiyya"], {"choice": "Affan Bhaiyya"}),
        ("cancel", ["Mumma", "Mumma Family"], {"cancelled": True}),
        ("what time is it", ["Mumma", "Mumma Family"], None),
    ],
)
def test_resolve_clarification_reply(reply, options, expected):
    assert resolve_clarification_reply(reply, options) == expected


# ── Server-side clarification lifecycle ────────────────────────────────────

class _ClarifyAudio:
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)


def test_pending_clarification_resumes_the_exact_send(monkeypatch):
    from backend.server import JarvisBackendService

    service = JarvisBackendService()
    audio = _ClarifyAudio()
    runtime = {"audio": audio}
    captured = []
    monkeypatch.setattr(service, "_run_action", lambda rt, text, plan=None: captured.append(plan))

    service._pending_clarification = {
        "candidates": ["Mumma", "Mumma Family"],
        "message": "hello there",
        "recipient": "mamma",
        "asked_at": __import__("time").monotonic(),
    }

    assert service._answer_clarification(runtime, "the second one") is True
    assert service._pending_clarification is None
    assert captured and captured[0]["steps"][0] == {
        "tool": "send_whatsapp",
        "recipient": "Mumma Family",
        "message": "hello there",
        "preferred": "Mumma Family",
    }


def test_pending_clarification_can_be_cancelled(monkeypatch):
    from backend.server import JarvisBackendService

    service = JarvisBackendService()
    audio = _ClarifyAudio()
    captured = []
    monkeypatch.setattr(service, "_run_action", lambda rt, text, plan=None: captured.append(plan))
    service._pending_clarification = {
        "candidates": ["Mumma"],
        "message": "hi",
        "recipient": "mamma",
        "asked_at": __import__("time").monotonic(),
    }

    assert service._answer_clarification({"audio": audio}, "never mind") is True
    assert captured == []
    assert audio.spoken == ["Okay, cancelled."]
    assert service._pending_clarification is None


def test_unrelated_utterance_drops_the_question(monkeypatch):
    """A new command must never be trapped by an old clarification question."""
    from backend.server import JarvisBackendService

    service = JarvisBackendService()
    captured = []
    monkeypatch.setattr(service, "_run_action", lambda rt, text, plan=None: captured.append(plan))
    service._pending_clarification = {
        "candidates": ["Mumma", "Mumma Family"],
        "message": "hi",
        "recipient": "mamma",
        "asked_at": __import__("time").monotonic(),
    }

    assert service._answer_clarification({"audio": _ClarifyAudio()}, "what time is it") is False
    assert captured == []
    assert service._pending_clarification is None


def test_expired_clarification_is_ignored(monkeypatch):
    from backend.server import JarvisBackendService

    service = JarvisBackendService()
    service._pending_clarification = {
        "candidates": ["Mumma"],
        "message": "hi",
        "recipient": "mamma",
        "asked_at": 0.0,
    }

    assert service._answer_clarification({"audio": _ClarifyAudio()}, "Mumma") is False
    assert service._pending_clarification is None


def test_whatsapp_manager_missing_recipient():
    manager = WhatsAppManager()
    res = manager.send_message("", "Hello")
    assert res.success is False
    assert "Recipient name is required" in res.message


# ── Permission Engine Tests ─────────────────────────────────────────────────

def test_permission_engine_evaluation():
    engine = PermissionEngine()

    # Direct explicit request for WhatsApp -> allowed immediately
    level, confirm = engine.evaluate("send_whatsapp", {"recipient": "Ahmed", "message": "Hi"}, is_explicit_user_request=True)
    assert level == PermissionLevel.NORMAL
    assert confirm is False

    # Open app -> allowed immediately
    level, confirm = engine.evaluate("open_app", {"app": "notepad"}, is_explicit_user_request=True)
    assert confirm is False

    # Destructive action -> requires confirmation
    level, confirm = engine.evaluate("delete_file", {"path": "C:\\important.doc"}, is_explicit_user_request=True)
    assert level == PermissionLevel.DESTRUCTIVE
    assert confirm is True


# ── Confirmation Store Lifecycle Tests ───────────────────────────────────────

@pytest.fixture
def temp_store():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test_perm.db"
        store = ConfirmationStore(db_path)
        yield store
        store.close()


def test_confirmation_store_pending_approval_reject_flow(temp_store):
    action_id = temp_store.create_pending_action(
        session_id="sess-100",
        tool="delete_file",
        payload={"path": "C:\\file.txt"},
        permission_level=PermissionLevel.DESTRUCTIVE,
    )

    action = temp_store.get_pending_action(action_id)
    assert action is not None
    assert action["status"] == ActionStatus.PENDING.value
    assert action["tool"] == "delete_file"

    pending_list = temp_store.list_pending_actions("sess-100")
    assert len(pending_list) == 1
    assert pending_list[0]["id"] == action_id

    # Approve action -> resumes the exact pending action through the runner
    class FakeRunner:
        def __init__(self):
            self.calls = []

        def run(self, call, task=None, confirmed=False):
            self.calls.append((call, confirmed))
            return ToolResult(call.tool, True, "executed", {"started": True, "completed": True})

    runner = FakeRunner()
    res = temp_store.approve_action(action_id, tool_runner=runner)
    assert res.success is True
    assert len(runner.calls) == 1
    call, confirmed = runner.calls[0]
    assert call.tool == "delete_file"
    assert call.payload == {"path": "C:\\file.txt"}
    assert confirmed is True  # resume path must not re-trigger the gate

    # Re-check status -> EXECUTED
    action_after = temp_store.get_pending_action(action_id)
    assert action_after["status"] == ActionStatus.EXECUTED.value

    # Pending list should now be empty
    assert len(temp_store.list_pending_actions("sess-100")) == 0


def test_approve_without_runner_does_not_fake_success(temp_store):
    action_id = temp_store.create_pending_action(
        session_id="sess-102",
        tool="delete_file",
        payload={"path": "x.txt"},
        permission_level=PermissionLevel.DESTRUCTIVE,
    )
    res = temp_store.approve_action(action_id)
    assert res.success is False


def test_confirmation_store_rejection(temp_store):
    action_id = temp_store.create_pending_action(
        session_id="sess-101",
        tool="system_shutdown",
        payload={},
        permission_level=PermissionLevel.DESTRUCTIVE,
    )

    rejected = temp_store.reject_action(action_id)
    assert rejected is True

    action = temp_store.get_pending_action(action_id)
    assert action["status"] == ActionStatus.REJECTED.value


def test_whatsapp_reports_failure_when_message_still_in_composer():
    """A typed-but-unsent draft must never be reported as delivered."""

    class FakeDesktop:
        def open_app(self, app):
            return True

    class FakeComputer:
        def focus_window(self, title):
            return True
        def hotkey(self, *keys):
            pass
        def press(self, key):
            pass
        def type_text(self, text):
            pass
        def set_clipboard(self, text):
            pass
        def paste(self):
            pass
        def get_active_window(self):
            return "WhatsApp"

    class FakeVision:
        def click_text(self, target):
            return True

        def read_header(self):
            return "WhatsApp  Ahmed"

        def read_composer(self):
            # The text is still in the input row -> Enter never sent it.
            return "Hello there"

        def read_screen(self):
            return {"window": "WhatsApp", "text": "Hello there"}

    manager = WhatsAppManager(
        desktop=FakeDesktop(), computer=FakeComputer(), vision=FakeVision()
    )
    res = manager.send_message("Ahmed", "Hello there")
    assert res.success is False
    assert res.data["verified"] is False
    assert res.data["evidence"] == "message_still_in_composer"


def test_whatsapp_verifies_when_composer_empty_and_message_visible():
    """Positive path: composer cleared and the bubble is visible -> verified."""

    class FakeDesktop:
        def open_app(self, app):
            return True

    class FakeComputer:
        def focus_window(self, title):
            return True
        def hotkey(self, *keys):
            pass
        def press(self, key):
            pass
        def type_text(self, text):
            pass
        def set_clipboard(self, text):
            pass
        def paste(self):
            pass
        def get_active_window(self):
            return "WhatsApp"

    class FakeVision:
        def click_text(self, target):
            return True

        def read_header(self):
            return "WhatsApp  Ahmed"

        def read_composer(self):
            return ""  # input row is clear after Enter

        def read_screen(self):
            return {"window": "WhatsApp", "text": "Ahmed Hello there"}

    manager = WhatsAppManager(
        desktop=FakeDesktop(), computer=FakeComputer(), vision=FakeVision()
    )
    res = manager.send_message("Ahmed", "Hello there")
    assert res.success is True
    assert res.data["evidence"] == "message_visible_in_conversation"
