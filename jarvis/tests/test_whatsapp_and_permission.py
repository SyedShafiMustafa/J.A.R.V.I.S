"""
tests/test_whatsapp_and_permission.py

Automated unit tests for WhatsApp messaging flow, parsing, verification,
and the permission engine / confirmation lifecycle.
"""

import tempfile
from pathlib import Path
import pytest

from tools.whatsapp import parse_whatsapp_request, WhatsAppManager
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
