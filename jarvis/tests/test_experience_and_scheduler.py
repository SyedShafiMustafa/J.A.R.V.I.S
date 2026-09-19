"""
tests/test_experience_and_scheduler.py

- experience memory: stored, retrieved, bounded, fed to the planner, irrelevant excluded
- scheduler surface: parse intents, persist across restart, dedupe, cancel
"""

import tempfile
import time
from pathlib import Path

import pytest

from core.memory import Memory
from core.scheduler import (
    TaskScheduler,
    parse_schedule_request,
    humanize_seconds,
)


# ── Experience memory ───────────────────────────────────────────────────────

@pytest.fixture
def memory():
    with tempfile.TemporaryDirectory() as tmp:
        mem = Memory(db_path=Path(tmp) / "mem.db")
        yield mem
        mem.conn.close()


def test_experience_is_stored_and_retrieved(memory):
    memory.save_experience(
        scenario="tool:open_app",
        strategy='{"app": "whatsapp"}',
        outcome="failure",
        lesson="start-menu alias launch needed for whatsapp",
    )
    results = memory.retrieve_experiences("open whatsapp please", limit=3)
    assert results, "relevant experience not retrieved"
    assert any("whatsapp" in r["lesson"].lower() for r in results)


def test_irrelevant_experience_is_excluded(memory):
    memory.save_experience("tool:open_app", "{}", "success", "notepad launch path works")
    assert memory.retrieve_experiences("what is the weather like", limit=3) == []


def test_experience_retrieval_is_bounded(memory):
    for i in range(10):
        memory.save_experience(f"tool:git_status:{i}", "{}", "success", "git status fine")
    results = memory.retrieve_experiences("git status", limit=3)
    assert len(results) <= 3


def test_planner_receives_bounded_lessons(monkeypatch):
    from agents.planner import TaskPlanner

    planner = TaskPlanner()

    captured = {}

    class FakeProvider:
        def complete(self, messages):
            captured["messages"] = messages
            return '{"goal":"g","steps":[{"tool":"git_status"}]}'

    planner.provider = FakeProvider()
    plan = planner.create_plan("check git", lessons=["tool:git_status -> success: works", "x", "y", "z"])
    assert plan["goal"] == "g"

    contents = " ".join(m["content"] for m in captured["messages"])
    assert "tool:git_status -> success: works" in contents
    # bounded to 3 lessons
    lesson_msgs = [m for m in captured["messages"] if "Operational lessons" in m["content"]]
    assert len(lesson_msgs) == 1
    assert lesson_msgs[0]["content"].count("- ") <= 3


def test_planner_without_lessons_has_no_lesson_message(monkeypatch):
    from agents.planner import TaskPlanner

    planner = TaskPlanner()
    captured = {}

    class FakeProvider:
        def complete(self, messages):
            captured["messages"] = messages
            return '{"goal":"g","steps":[{"tool":"git_status"}]}'

    planner.provider = FakeProvider()
    planner.create_plan("check git")
    assert not any("Operational lessons" in m["content"] for m in captured["messages"])


def test_orchestrator_attaches_experiences_to_action_decision():
    from backend.live_adapters import LiveOrchestrator

    orch = LiveOrchestrator()

    class FakeMemory:
        def retrieve_experiences(self, text, limit=3):
            return [{"scenario": "tool:open_app", "outcome": "failure", "lesson": "use alias"}]

        def search_memories(self, text):
            return []

    orch.memory = FakeMemory()
    # "send ..." bypasses the simple router and reaches the planner path.
    decision = orch.decide("send an email to bob")
    assert decision.kind == "action"
    assert decision.metadata["experiences"][0]["lesson"] == "use alias"


# ── Scheduler surface ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,kind,value_key,value",
    [
        ("remind me in 10 minutes to call mom", "remind", "delay_seconds", 600),
        ("remind me to stretch in 30 seconds", "remind", "delay_seconds", 30),
        ("remind me every 5 minutes to drink water", "recurring", "interval_seconds", 300),
        ("every morning remind me to check mail", "recurring", "interval_seconds", 86400),
        ("cancel my reminders", "cancel", None, None),
    ],
)
def test_parse_schedule_request(text, kind, value_key, value):
    parsed = parse_schedule_request(text)
    assert parsed is not None
    assert parsed["kind"] == kind
    if value_key:
        assert parsed[value_key] == value


@pytest.mark.parametrize("text", ["open notepad", "what time is it", "", "tell me a joke"])
def test_parse_schedule_request_ignores_non_schedules(text):
    assert parse_schedule_request(text) is None


def test_humanize_seconds():
    assert humanize_seconds(30) == "30 seconds"
    assert humanize_seconds(600) == "10 minutes"
    assert humanize_seconds(7200) == "2 hours"


def test_scheduler_persists_across_restart():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sched.db"
        s1 = TaskScheduler(db_path=db)
        task_id = s1.schedule_task("reminder", {"remind": "call mom"}, delay_seconds=3600)
        s1.close()

        s2 = TaskScheduler(db_path=db)
        try:
            tasks = s2.list_scheduled_tasks()
            assert any(t["id"] == task_id for t in tasks)
        finally:
            s2.close()


def test_scheduler_duplicate_prevention():
    with tempfile.TemporaryDirectory() as tmp:
        sched = TaskScheduler(db_path=Path(tmp) / "sched.db")
        try:
            a = sched.schedule_task("reminder", {"remind": "water"}, delay_seconds=3600)
            b = sched.schedule_task("reminder", {"remind": "water"}, delay_seconds=9999)
            assert a == b
            assert len(sched.list_scheduled_tasks()) == 1
        finally:
            sched.close()


def test_scheduler_cancel():
    with tempfile.TemporaryDirectory() as tmp:
        sched = TaskScheduler(db_path=Path(tmp) / "sched.db")
        try:
            tid = sched.schedule_task("reminder", {"remind": "water"}, delay_seconds=3600)
            assert sched.cancel_task(tid) is True
            assert sched.list_scheduled_tasks() == []
            assert sched.cancel_task(tid) is False
        finally:
            sched.close()


def test_scheduler_runs_due_task_only_once():
    with tempfile.TemporaryDirectory() as tmp:
        sched = TaskScheduler(db_path=Path(tmp) / "sched.db")
        seen = []
        sched.set_action_handler(lambda name, payload: seen.append(payload))
        sched.start()
        try:
            sched.schedule_task("reminder", {"remind": "now"}, delay_seconds=0.0)
            deadline = time.time() + 4
            while not seen and time.time() < deadline:
                time.sleep(0.1)
            assert len(seen) == 1
            # a one-shot task must not fire again
            time.sleep(1.5)
            assert len(seen) == 1
        finally:
            sched.close()
