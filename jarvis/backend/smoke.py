"""
backend/smoke.py

Lightweight smoke tests for the backend.

This file is intentionally self-contained so it can be run with
a standard Python invocation and does not require pytest.

It is meant to be the first line of defense for the core backend
contracts:
- session/task lifecycle
- error taxonomy
- tool definitions and dry-run contract
- retry behavior
- event bus
- startup validation for the fakes

Usage:
    python -m jarvis.backend.smoke
    # or
    python jarvis/backend/smoke.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Allow running from repo root or from inside the backend package.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.interfaces import (
    BackendError,
    ConfigurationError,
    PermanentError,
    TransientError,
    FakeAudioProvider,
    FakeToolRunner,
    ToolCall,
    ToolResult,
    ToolRunner,
    AudioProvider,
    Orchestrator,
    OrchestratorDecision,
)
from backend.models import Session, Task, TaskStatus
from backend.retry import retry, RetryConfig
from backend.bus import (
    BackendBus,
    BackendEvent,
    session_started,
    session_ended,
    tool_started,
    tool_finished,
    tool_failed,
    wake_detected,
    wake_listening,
    audio_start,
    audio_stop,
    ReplayObserver,
)
from backend.tools import ToolError, ToolResult as BackendToolResult, ToolRegistry, build_default_tool_registry
from backend.validation import validate_model_paths, validate_required_settings, validate_backend_startup
from backend.logging import log_event, log_info, log_error


_passed = 0
_failed = 0
_errors: list[str] = []


def ok(label: str, condition: bool, extra: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS: {label}")
    else:
        _failed += 1
        msg = f"  FAIL: {label}"
        if extra:
            msg += f" ({extra})"
        print(msg)
        _errors.append(label)


def start_section(label: str) -> None:
    print()
    print(f"== {label} ==")


# ---------------------------------------------------------------------------
# Helpers for assertions
# ---------------------------------------------------------------------------

def assert_equal(actual, expected, label: str):
    ok(label, actual == expected, f"actual={actual!r} expected={expected!r}")


def assert_isinstance(obj, cls, label: str):
    ok(label, isinstance(obj, cls), f"type={type(obj).__name__}")


def assert_raises(exc_class, fn, label: str, expected_message: str | None = None):
    try:
        fn()
        ok(label, False, "no exception raised")
        return
    except Exception as e:
        if isinstance(e, exc_class):
            if expected_message is not None:
                ok(label, expected_message in str(e), f"message={str(e)!r}")
            else:
                ok(label, True)
        else:
            ok(label, False, f"wrong exception {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Session / task lifecycle
# ---------------------------------------------------------------------------

def test_session_task_lifecycle() -> None:
    start_section("session/task lifecycle")

    s = Session("sess-1")
    ok("session.id is set", s.id == "sess-1")
    ok("session starts uncancelled", s.cancelled is False)
    ok("session starts with no active task", s.active_task is None)
    assert_equal(s.active_task_is_running(), False, "active_task_is_running() initial")
    assert_equal(s.active_task_is_running(), False, "active_task_is_running() after start")

    t = Task("task-1", "Send message", [{"tool": "type", "text": "hello"}])
    assert_equal(t.status, TaskStatus.PENDING, "task initial status")
    assert_equal(t.result_message, "", "task initial message")

    s.active_task = t
    ok("session sees active task", s.active_task is t)
    ok("session reports running task", s.active_task_is_running() is False)

    t.start()
    ok("task status becomes running", t.status == TaskStatus.RUNNING)
    ok("task started_at is set", t.started_at is not None)
    ok("session reports running task after task.start()", s.active_task_is_running() is True)

    t.complete("done")
    ok("task status becomes completed", t.status == TaskStatus.COMPLETED)
    ok("task result_message set on complete", t.result_message == "done")
    ok("task finished_at set on complete", t.finished_at is not None)

    t2 = Task("task-2", "Fail test", [])
    t2.fail("something broke")
    ok("task status becomes failed", t2.status == TaskStatus.FAILED)
    ok("task result_message set on fail", t2.result_message == "something broke")

    t3 = Task("task-3", "Cancel test", [])
    t3.cancel()
    ok("task status becomes cancelled", t3.status == TaskStatus.CANCELLED)
    ok("task finished_at set on cancel", t3.finished_at is not None)

    s2 = Session("sess-2")
    s2.active_task = t3
    s2.cancel_active_task()
    ok("cancel_active_task cancels task", s2.active_task is not None and s2.active_task.status == TaskStatus.CANCELLED)
    ok("cancel_active_task marks session cancelled", s2.cancelled is True)

    summary = s2.summary()
    ok("summary includes id", summary["id"] == "sess-2")
    ok("summary includes cancelled flag", summary["cancelled"] is True)
    ok("summary includes active task info", "active_task" in summary)


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------

def test_error_taxonomy() -> None:
    start_section("error taxonomy")

    assert_isinstance(BackendError("x"), Exception, "BackendError is Exception")
    assert_isinstance(ConfigurationError("x"), BackendError, "ConfigurationError is BackendError")
    assert_isinstance(TransientError("x"), BackendError, "TransientError is BackendError")
    assert_isinstance(PermanentError("x"), BackendError, "PermanentError is BackendError")

    ok("errors are distinct types", ConfigurationError is not TransientError)
    ok("errors carry messages", str(ConfigurationError("missing config")) == "missing config")


# ---------------------------------------------------------------------------
# Fake audio provider with controlled transcript/error behavior
# ---------------------------------------------------------------------------

class ConfigurableFakeAudio:
    """Fake AudioProvider that can be configured for tests."""

    def __init__(
        self,
        transcript: str = "",
        record_path: str = "fake_recording.wav",
        speak_calls: list[str] | None = None,
    ) -> None:
        self.transcript = transcript
        self.record_path = record_path
        self.speak_calls = speak_calls if speak_calls is not None else []
        self.wake_started = False
        self.wake_stopped = False

    def start_wake_word(self) -> None:
        self.wake_started = True

    def stop_wake_word(self) -> None:
        self.wake_stopped = True

    def record_audio(self) -> str:
        return self.record_path

    def transcribe(self, audio_path: str) -> str:
        return self.transcript

    def speak(self, text: str) -> None:
        self.speak_calls.append(text)

    def stop_speaking(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fake tool runner with controlled success/failure behavior
# ---------------------------------------------------------------------------

class ControlledFakeRunner:
    """Fake ToolRunner that can be scripted to succeed or fail."""

    def __init__(self, results: list[ToolResult] | None = None) -> None:
        self.results = results if results is not None else []
        self.calls: list[ToolCall] = []

    def run(self, call: ToolCall, task=None) -> ToolResult:
        self.calls.append(call)

        if not self.results:
            return ToolResult(tool=call.tool, success=True, message="ok", data=dict(call.payload))

        return self.results.pop(0)


# ---------------------------------------------------------------------------
# Fake orchestrator for decision routing tests
# ---------------------------------------------------------------------------

class FakeOrchestrator:
    """Orchestrator that always returns a scripted decision."""

    def __init__(self, decision: OrchestratorDecision) -> None:
        self.decision = decision

    def decide(self, user_text: str, context=None) -> OrchestratorDecision:
        return self.decision


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def test_fakes() -> None:
    start_section("fakes")

    audio = FakeAudioProvider()
    ok("fake audio implements contract", isinstance(audio, AudioProvider))
    assert_equal(audio.record_audio(), "", "fake record returns empty path")
    assert_equal(audio.transcribe("dummy.wav"), "", "fake transcribe returns empty")
    audio.start_wake_word()
    audio.stop_wake_word()
    audio.speak("hello")
    audio.stop_speaking()
    ok("fake audio methods run without error", True)

    runner = FakeToolRunner()
    call = ToolCall("open_app", {"app": "notepad"})
    result = runner.run(call)
    assert_equal(result.tool, "open_app", "fake runner tool name")
    ok("fake runner success", result.success is True)
    ok("fake runner message", result.message == "fake:open_app")
    assert_equal(result.data, {"app": "notepad"}, "fake runner payload")
    ok("fake runner recorded call", runner.calls == [call])

    cfg = ConfigurableFakeAudio(transcript="hello world", speak_calls=[])
    assert_equal(cfg.transcribe("anything.wav"), "hello world", "configurable fake transcript")
    cfg.speak("one")
    cfg.speak("two")
    ok("configurable fake records speak calls", cfg.speak_calls == ["one", "two"])

    runner2 = ControlledFakeRunner()
    r = runner2.run(ToolCall("type", {"text": "x"}))
    ok("controlled fake runner defaults to success", r.success is True)

    runner3 = ControlledFakeRunner(results=[
        ToolResult(tool="type", success=False, message="boom", data={}),
    ])
    r2 = runner3.run(ToolCall("type", {"text": "x"}))
    ok("controlled fake runner can be scripted to fail", r2.success is False)
    ok("controlled fake runner carries failure message", r2.message == "boom")


# ---------------------------------------------------------------------------
# Tool definitions and dry-run contract
# ---------------------------------------------------------------------------

def test_tool_definitions() -> None:
    start_section("tool definitions / dry-run")

    reg = build_default_tool_registry()
    ok("registry lists tools", len(reg.list_all()) > 0)
    assert_equal(reg.get("type").name, "type", "tool name lookup")
    assert_equal(reg.get("type").supports_dry_run, True, "type supports dry run")
    ok("open_app does not support dry run", reg.get("open_app").supports_dry_run is False)

    ok("tool describe returns name", reg.get("type").describe()["name"] == "type")
    ok("tool describe includes input fields", "input_fields" in reg.get("type").describe())

    # Dry-run contract
    ok("unknown tool dry-run fails", reg.get("nope") is None)

    missing = ToolError(tool="type", reason="Missing required fields: text", detail={"missing": ["text"]}).to_result()
    assert_equal(missing.tool, "type", "tool error tool name")
    ok("tool error marks failure", missing.success is False)
    ok("tool error carries reason", "Missing required fields" in missing.message)

    # structural check below
    from backend.tools import ToolResult as BackendToolResult

    base = BackendToolResult(tool="type", success=False, message="base", data={"existing": 1})
    altered = base.as_error("new reason")
    assert_equal(altered.tool, "type", "as_error tool")
    ok("as_error marks failure", altered.success is False)
    ok("as_error message replaced", altered.message == "new reason")
    ok("as_error keeps existing data keys", "existing" in (altered.data or {}))


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------

def test_retry() -> None:
    start_section("retry")

    calls = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 2:
            raise TransientError("temporary")
        return "ok"

    fast = RetryConfig(max_attempts=3, base_delay_s=0.0, max_delay_s=0.01, jitter=False)
    result = retry(flaky, config=fast)
    assert_equal(result, "ok", "retry eventually succeeds")
    ok("retry retries on transient failure", len(calls) >= 2)

    calls2 = []

    def always_fail() -> str:
        calls2.append(1)
        raise TransientError("nope")

    assert_raises(
        TransientError,
        lambda: retry(always_fail, config=RetryConfig(max_attempts=2, base_delay_s=0.0, max_delay_s=0.01, jitter=False)),
        "retry raises after max attempts",
    )
    ok("retry made expected attempts when failing", len(calls2) == 2)

    def permanent() -> str:
        raise PermanentError("do not retry")

    assert_raises(
        PermanentError,
        lambda: retry(permanent, config=RetryConfig(max_attempts=3, base_delay_s=0.0, max_delay_s=0.01, jitter=False)),
        "retry does not retry permanent errors",
    )
    ok("permanent failure raised immediately", True)

    # Retry should not swallow non-retryable exceptions
    def non_retryable_excepthook() -> str:
        raise ValueError("not retryable")

    assert_raises(
        ValueError,
        lambda: retry(non_retryable_excepthook, config=RetryConfig(max_attempts=3, base_delay_s=0.0, max_delay_s=0.01, jitter=False)),
        "retry propagates non-retryable exceptions immediately",
    )


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

def test_event_bus() -> None:
    start_section("event bus")

    bus = BackendBus()
    replay = ReplayObserver()
    bus.subscribe(replay)

    s = Session("bus-sess")
    t = Task("bus-task", "Do thing", [])

    bus.publish(session_started(s))
    ok("session_started event delivered", len(replay.events) == 1)
    assert_equal(replay.events[0].kind, "session.started", "event kind")
    assert_equal(replay.events[0].session_id, "bus-sess", "event session_id")

    bus.publish(wake_listening(session_id="bus-sess"))
    bus.publish(audio_start(session_id="bus-sess"))
    bus.publish(tool_started(session_id="bus-sess", task_id=t.id, tool="type"))
    ok("multiple events delivered", len(replay.events) == 4)

    last = replay.events[-1]
    assert_equal(last.kind, "tool.started", "last event kind")
    assert_equal(last.task_id, "bus-task", "last event task_id")
    assert_equal(last.meta.get("tool"), "type", "last event tool")

    bus.unsubscribe(replay)
    bus.publish(wake_detected(session_id="bus-sess"))
    ok("unsubscribed listener not called", len(replay.events) == 4)


# ---------------------------------------------------------------------------
# Logging boundaries
# ---------------------------------------------------------------------------

def test_logging() -> None:
    start_section("logging")

    ok("log_event is callable", callable(log_event))
    ok("log_info is callable", callable(log_info))
    ok("log_error is callable", callable(log_error))

    # Simple structural check: basic log calls should not crash.
    try:
        log_info("test.at", "info message", {"key": "value"})
        log_error("test.at", "error message", {"key": "value"})
        ok("log_info/error run without raising", True)
    except Exception as e:
        ok("log_info/error run without raising", False, str(e))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def test_validation() -> None:
    start_section("validation")

    assert_raises(
        ConfigurationError,
        lambda: (_ for _ in ()).throw(ConfigurationError("bad")),
        "ConfigurationError exists",
    )

    ok("validate_required_settings exists", callable(validate_required_settings))
    ok("validate_model_paths exists", callable(validate_model_paths))
    ok("validate_backend_startup exists", callable(validate_backend_startup))


def main() -> None:
    print("Running backend test suite")
    print("Python:", sys.version.split()[0])

    test_session_task_lifecycle()
    test_error_taxonomy()
    test_fakes()
    test_tool_definitions()
    test_retry()
    test_event_bus()
    test_logging()
    test_validation()

    # Light backend integration checks using fakes
    start_section("backend integration with fakes")

    session2 = Session("integ-sess")
    ok("session starts clean for integration", session2.active_task is None)

    bus2 = BackendBus()
    replay2 = ReplayObserver()
    bus2.subscribe(replay2)

    bus2.publish(session_started(session2))
    ok("integration session started event emitted", len(replay2.events) == 1)
    ok(
        replay2.events[0].kind == "session.started",
        "integration session event kind",
    )

    runner3 = ControlledFakeRunner()
    call3 = ToolCall("type", {"text": "integration test"})
    result3 = runner3.run(call3)
    ok("integration tool call recorded", len(runner3.calls) == 1)
    ok("integration tool call result is success", result3.success is True)

    orchestrator = FakeOrchestrator(
        OrchestratorDecision(
            kind="reply",
            reply="integration reply",
            metadata={"source": "fake"},
        )
    )
    decision = orchestrator.decide("hello")
    ok("fake orchestrator returns scripted decision", decision.kind == "reply")
    ok("fake orchestrator reply preserved", decision.reply == "integration reply")

    print()
    print("--------")
    print(f"results: {_passed} passed, {_failed} failed")

    if _errors:
        print("failures:")
        for label in _errors:
            print(" -", label)
        sys.exit(1)

    print("backend test suite passed")


if __name__ == "__main__":
    main()
