"""
tests/test_agent_wiring.py

Tests for the agent-integration wiring:
- planner exposes and validates the new tool set
- permission gate persists + resumes sensitive actions (no inline execution)
- WhatsApp deterministic intent parsing/routing
- emergency-stop phrase recognition
- scheduler invokes its action handler
"""

import tempfile
import time
from pathlib import Path

import pytest

from agents.planner import TaskPlanner
from agents.ollama_errors import PlannerValidationError
from backend.bus import BackendBus
from backend.interfaces import ToolCall, ToolResult
from backend.live_adapters import LiveToolRunner
from core.permission import ConfirmationStore
from core.scheduler import TaskScheduler


# ── Planner tool exposure ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    "plan",
    [
        {"goal": "g", "steps": [{"tool": "send_whatsapp", "recipient": "Ahmed", "message": "hi"}]},
        {"goal": "g", "steps": [{"tool": "create_file", "path": "a.txt", "content": "x"}]},
        {"goal": "g", "steps": [{"tool": "create_file", "path": "a.txt"}]},  # optional field omitted
        {"goal": "g", "steps": [{"tool": "execute_terminal", "command": "dir", "timeout": 5}]},
        {"goal": "g", "steps": [{"tool": "run_pytest", "test_path": "core/tests"}]},
        {"goal": "g", "steps": [{"tool": "git_status"}]},
    ],
)
def test_planner_accepts_new_tools(plan):
    TaskPlanner._validate_plan(plan)


@pytest.mark.parametrize(
    "plan",
    [
        {"goal": "g", "steps": [{"tool": "not_a_tool"}]},
        {"goal": "g", "steps": [{"tool": "send_whatsapp", "recipient": "Ahmed"}]},  # missing message
        {"goal": "g", "steps": [{"tool": "git_status", "rogue": "x"}]},  # unexpected field
        {"goal": "g", "steps": [{"tool": "delete_file"}]},  # missing required path
        {"goal": "g", "steps": [{"tool": "execute_terminal", "command": "dir", "timeout": -1}]},
    ],
)
def test_planner_rejects_invalid_new_tool_steps(plan):
    with pytest.raises(PlannerValidationError):
        TaskPlanner._validate_plan(plan)


# ── Permission gate + resume ────────────────────────────────────────────────

class _Bus(BackendBus):
    pass


@pytest.fixture
def perm_env():
    with tempfile.TemporaryDirectory() as tmp:
        store = ConfirmationStore(db_path=Path(tmp) / "perm.db")
        from core.permission import PermissionEngine
        runner = LiveToolRunner(
            bus=BackendBus(),
            session_id="sess-wire",
            permission_engine=PermissionEngine(),
            confirmation_store=store,
        )
        yield runner, store
        store.close()


def test_destructive_tool_is_blocked_and_persisted(perm_env):
    runner, store = perm_env
    target = Path(tempfile.mkdtemp()) / "doomed.txt"
    target.write_text("bye", encoding="utf-8")

    result = runner.run(ToolCall(tool="delete_file", payload={"path": str(target)}))

    assert result.success is False
    assert result.data["confirmation_required"] is True
    assert result.data["action_id"]
    # Not executed: file still there, and a pending action exists.
    assert target.exists()
    pending = store.list_pending_actions("sess-wire")
    assert len(pending) == 1
    assert pending[0]["tool"] == "delete_file"


def test_approved_action_is_resumed_and_executed(perm_env):
    runner, store = perm_env
    target = Path(tempfile.mkdtemp()) / "doomed.txt"
    target.write_text("bye", encoding="utf-8")

    blocked = runner.run(ToolCall(tool="delete_file", payload={"path": str(target)}))
    action_id = blocked.data["action_id"]

    resumed = store.approve_action(action_id, tool_runner=runner)
    assert resumed.success is True
    assert not target.exists()  # actually executed, and no re-block


def test_rejected_action_is_not_executed(perm_env):
    runner, store = perm_env
    target = Path(tempfile.mkdtemp()) / "keep.txt"
    target.write_text("keep", encoding="utf-8")

    blocked = runner.run(ToolCall(tool="delete_file", payload={"path": str(target)}))
    action_id = blocked.data["action_id"]

    assert store.reject_action(action_id) is True
    assert target.exists()
    assert store.list_pending_actions("sess-wire") == []


def test_normal_tool_executes_without_confirmation(perm_env):
    runner, _ = perm_env
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "created.txt"
        result = runner.run(ToolCall(tool="create_file", payload={"path": str(target), "content": "yo"}))
        assert result.success is True
        assert target.read_text(encoding="utf-8") == "yo"


# ── WhatsApp routing + emergency stop ───────────────────────────────────────

def test_whatsapp_plan_parses_explicit_request():
    from backend.server import JarvisBackendService

    plan = JarvisBackendService._whatsapp_plan("Send Ahmed 'I am reaching in 10 minutes' on WhatsApp")
    assert plan is not None
    step = plan["steps"][0]
    assert step["tool"] == "send_whatsapp"
    assert step["recipient"] == "Ahmed"
    assert step["message"] == "I am reaching in 10 minutes"


def test_whatsapp_plan_ignores_unrelated_text():
    from backend.server import JarvisBackendService

    assert JarvisBackendService._whatsapp_plan("open chrome and search news") is None


@pytest.mark.parametrize("text", ["stop", "Stop it.", "Jarvis, stop.", "emergency stop", "abort that"])
def test_emergency_stop_phrase_positive(text):
    from backend.server import _is_emergency_stop_phrase

    assert _is_emergency_stop_phrase(text) is True


@pytest.mark.parametrize("text", ["stop listening", "what time is it", "do not stop the music please"])
def test_emergency_stop_phrase_negative(text):
    from backend.server import _is_emergency_stop_phrase

    assert _is_emergency_stop_phrase(text) is False


# ── Scheduler wiring ────────────────────────────────────────────────────────

def test_scheduler_invokes_action_handler():
    with tempfile.TemporaryDirectory() as tmp:
        sched = TaskScheduler(db_path=Path(tmp) / "sched.db")
        seen = []
        sched.set_action_handler(lambda name, payload: seen.append((name, payload)))
        sched.start()
        try:
            sched.schedule_task("run_pytest", {"tool": "run_pytest"}, delay_seconds=0.0)
            deadline = time.time() + 4.0
            while not seen and time.time() < deadline:
                time.sleep(0.1)
            assert seen, "scheduled task never fired"
            assert seen[0][0] == "run_pytest"
        finally:
            sched.close()
