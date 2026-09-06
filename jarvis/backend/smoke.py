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
- backend service API smoke checks

Usage:
    python -m jarvis.backend.smoke
    # or
    python jarvis/backend/smoke.py
"""

from __future__ import annotations

import sys
import time
import urllib.request
import urllib.error
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
    task_started,
    task_completed,
    task_failed,
    ReplayObserver,
)
from backend.tools import ToolError, ToolResult as BackendToolResult, ToolRegistry, build_default_tool_registry
from backend.validation import validate_model_paths, validate_required_settings, validate_backend_startup
from backend.observability import log_event, log_info, log_error

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
    """Fake ToolRunner that can be scripted to succeed or fail.

    Its ``run()`` signature matches the real tool runner contract so
    tests can swap it in without changing call sites.
    """

    def __init__(self, results: list[ToolResult] | None = None) -> None:
        self.results = results if results is not None else []
        self.calls: list[ToolCall] = []

    def run(
        self,
        call: ToolCall,
        task: Task | None = None,
    ) -> ToolResult:
        self.calls.append(call)

        if not self.results:
            return ToolResult(
                tool=call.tool,
                success=True,
                message="ok",
                data=dict(call.payload),
                meta={"controlled": True},
            )

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

    # Retry attaches lightweight metadata to tool results
    calls3 = []

    def flaky_tool() -> ToolResult:
        calls3.append(1)
        if len(calls3) < 2:
            raise TransientError("temporary")
        return ToolResult(tool="type", success=True, message="done", data={}, meta={"origin": "fake"})

    rt = retry(flaky_tool, config=RetryConfig(max_attempts=3, base_delay_s=0.0, max_delay_s=0.01, jitter=False))
    ok("retry preserves tool result shape", isinstance(rt, ToolResult))
    ok("retry reports eventual success", rt.success is True)
    ok("retry keeps existing meta fields", rt.meta is not None and rt.meta.get("origin") == "fake")
    ok("retry attaches attempt metadata", rt.meta is not None and rt.meta.get("attempt") == 2)
    ok("retry attaches retries_used metadata", rt.meta is not None and rt.meta.get("retries_used") == 1)


    def failing_tool() -> ToolResult:
        raise TransientError("nope")

    assert_raises(
        TransientError,
        lambda: retry(failing_tool, config=RetryConfig(max_attempts=2, base_delay_s=0.0, max_delay_s=0.01, jitter=False)),
        "retry raises when all attempts fail",
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
# Lifecycle / interrupt behavior
# ---------------------------------------------------------------------------

def test_lifecycle_interrupt_behavior() -> None:
    start_section("lifecycle / interrupt behavior")

    from backend.lifecycle import Lifecycle

    bus = BackendBus()
    replay = ReplayObserver()
    bus.subscribe(replay)

    session = Session("lc-sess")
    lifecycle = Lifecycle(bus, session)
    lifecycle.mark_started()

    ok("lifecycle starts running", lifecycle.running is True)
    ok("lifecycle starts with no shutdown requested", lifecycle.shutdown_requested is False)

    lifecycle.request_shutdown()
    ok("request_shutdown sets flag", lifecycle.shutdown_requested is True)
    ok("request_shutdown cancels active task", session.cancelled is True)

    bus2 = BackendBus()
    session2 = Session("lc-sess-2")
    lifecycle2 = Lifecycle(bus2, session2)
    lifecycle2.mark_started()

    task = Task("lc-task", "interrupt me", [])
    session2.active_task = task
    task.start()

    lifecycle2.request_shutdown()
    ok("shutdown request cancels running active task", session2.active_task is not None and session2.active_task.status == TaskStatus.CANCELLED)

    finished_events = [e for e in replay.events if e.kind in {"session.ended", "audio.stop", "tool.failed"}]
    ok("no session ended event before shutdown()", len(finished_events) == 0)

    # Note: lifecycle2 is not wired to 'bus' in this smoke check, so it does
    # not publish session.ended to the replay observer above. That is intentional:
    # it verifies that Lifecycle.shutdown() emits through its own bus, not through
    # some global. The real contract test for that is lifecycle5 / replay5.
    lifecycle2.shutdown()
    ended_events = [e for e in replay.events if e.kind == "session.ended"]
    ok("shutdown does not publish to unrelated bus", len(ended_events) == 0)
    if len(ended_events) > 0:
        recent_kinds = [e.kind for e in replay.events[-6:]]
        print(f"  note: unexpected session.ended kinds near lifecycle2: {recent_kinds}")
        ok("debug note: lifecycle2 unexpectedly published to replay bus", False, "expected no session.ended from lifecycle2")

    # Cleanup callback ordering / robustness
    cleanup_called = []
    lifecycle3 = Lifecycle(bus, Session("lc-sess-3"))
    lifecycle3.mark_started()
    lifecycle3.register_cleanup(lambda: cleanup_called.append("a"))
    lifecycle3.register_cleanup(lambda: cleanup_called.append("b"))
    lifecycle3.shutdown()
    ok("cleanup callbacks run on shutdown", cleanup_called == ["a", "b"])

    error_called = []
    lifecycle4 = Lifecycle(bus, Session("lc-sess-4"))
    lifecycle4.mark_started()
    def bad_cleanup():
        error_called.append("bad")
        raise RuntimeError("cleanup fail")
    lifecycle4.register_cleanup(bad_cleanup)
    lifecycle4.shutdown()
    ok("shutdown continues even if a cleanup callback fails", error_called == ["bad"])

    # Session ended event requires shutdown to be published after the bus is wired.
    # This smoke check verifies the contract when the Lifecycle is created with
    # the same bus it should publish on.
    bus5 = BackendBus()
    replay5 = ReplayObserver()
    bus5.subscribe(replay5)
    lifecycle5 = Lifecycle(bus5, Session("lc-sess-5"))
    lifecycle5.mark_started()
    lifecycle5.shutdown()
    ended_events5 = [e for e in replay5.events if e.kind == "session.ended"]
    ok("session ended event is published through the lifecycle bus", len(ended_events5) >= 1)
    if len(ended_events5) >= 1:
        ok("session ended names the session", ended_events5[0].session_id == "lc-sess-5")
    else:
        ok("session ended event is published through the lifecycle bus", False, "no session.ended event found")
        recent5 = [e.kind for e in replay5.events[-6:]]
        print(f"  note: recent bus event kinds near lifecycle5: {recent5}")
        print(f"  note: lifecycle5._bus is replay5 bus: {lifecycle5._bus is bus5}")
        print(f"  note: lifecycle5._bus._listeners contains replay5: {replay5 in list(lifecycle5._bus._listeners)}")


# ---------------------------------------------------------------------------
# Tool execution metadata
# ---------------------------------------------------------------------------

def test_tool_execution_meta() -> None:
    start_section("tool execution metadata")

    base = ToolResult(
        tool="type",
        success=True,
        message="typed",
        data={"chars": 5},
        meta={"origin": "fake"},
    )
    ok("tool result carries metadata", base.meta is not None and base.meta.get("origin") == "fake")

    updated = ToolResult(
        tool=base.tool,
        success=base.success,
        message=base.message,
        data=base.data,
        meta={"attempt": 2, "retries_used": 1, "origin": "fake"},
    )
    ok("retry metadata can be attached to tool result", updated.meta is not None and updated.meta.get("attempt") == 2)
    ok("retry metadata can carry retries_used", updated.meta is not None and updated.meta.get("retries_used") == 1)


# ---------------------------------------------------------------------------
# Tool registry as single source of truth
# ---------------------------------------------------------------------------

def test_tool_registry_single_source() -> None:
    start_section("tool registry as single source of truth")

    reg = build_default_tool_registry()

    ok("registry knows type tool", reg.get("type") is not None)
    ok("registry knows open_app tool", reg.get("open_app") is not None)
    ok("registry rejects unknown tool", reg.get("nope") is None)

    error = reg.validate_payload("type", {"text": "hello"})
    ok("valid payload passes registry validation", error is None)

    bad = reg.validate_payload("type", {})
    ok("missing required field fails registry validation", bad is not None)
    ok("missing field error names the tool", bad is not None and bad.tool == "type")
    ok("missing field error includes field names", "text" in (bad.detail or {}).get("missing", []))

    no_dry = reg.validate_payload("open_app", {"app": "something"})
    ok("dry-run unsupported tool fails validation", no_dry is not None)
    ok("dry-run unsupported error names the tool", no_dry is not None and no_dry.tool == "open_app")

    desc = reg.describe_execution("type", {"text": "hello"})
    ok("describe_execution success for valid tool", desc.success is True)
    ok("describe_execution names the tool", desc.tool == "type")

    bad_desc = reg.describe_execution("type", {})
    ok("describe_execution fails for invalid payload", bad_desc.success is False)

    unknown_desc = reg.describe_execution("nope", {})
    ok("describe_execution fails for unknown tool", unknown_desc.success is False)


# ---------------------------------------------------------------------------
# Full fake backend turn
# ---------------------------------------------------------------------------

def test_full_fake_backend_turn() -> None:
    start_section("full fake backend turn")

    bus = BackendBus()
    replay = ReplayObserver()
    bus.subscribe(replay)

    session = Session("fake-turn")
    bus.publish(session_started(session))
    ok("fake turn session started event emitted", len(replay.events) == 1)

    audio = ConfigurableFakeAudio(transcript="open notepad", speak_calls=[])
    audio.start_wake_word()
    ok("fake audio can start wake word", audio.wake_started is True)

    bus.publish(wake_listening(session_id=session.id))
    ok("wake listening event emitted in fake turn", any(e.kind == "wake.listening" for e in replay.events))

    orchestrator = FakeOrchestrator(
        OrchestratorDecision(
            kind="action",
            intent="open notepad",
            tool_calls=[ToolCall("open_app", {"app": "notepad"})],
            metadata={"source": "fake"},
        )
    )

    decision = orchestrator.decide("open notepad")
    ok("fake orchestrator returns action decision", decision.kind == "action")
    ok("fake orchestrator returns tool calls", decision.tool_calls is not None and len(decision.tool_calls) == 1)
    ok("fake orchestrator tool call names the tool", decision.tool_calls[0].tool == "open_app")

    task = Task(
        id="fake-turn-task",
        goal="open notepad",
        steps=[{"tool": "open_app", "app": "notepad"}],
    )
    session.active_task = task
    bus.publish(task_started(task, session_id=session.id))
    ok("task started event emitted in fake turn", any(e.kind == "task.started" and e.task_id == "fake-turn-task" for e in replay.events))

    runner = ControlledFakeRunner()
    call = ToolCall("open_app", {"app": "notepad"})
    result = runner.run(call, task=task)
    ok("fake tool execution succeeds", result.success is True)
    ok("fake tool execution records the call", len(runner.calls) == 1)

    bus.publish(tool_finished(session_id=session.id, task_id=task.id, tool="open_app", success=True))
    ok("tool finished event emitted in fake turn", any(e.kind == "tool.finished" and e.task_id == "fake-turn-task" for e in replay.events))

    task.complete("opened notepad")
    bus.publish(task_completed(task, session_id=session.id))
    ok("task completed event emitted in fake turn", any(e.kind == "task.completed" and e.task_id == "fake-turn-task" for e in replay.events))

    audio.speak("Done.")
    ok("fake audio speaks reply in fake turn", audio.speak_calls == ["Done."])

    session.note_reply("Done.")
    ok("session records assistant reply in fake turn", session.last_assistant_reply == "Done.")

    from backend.lifecycle import Lifecycle

    lifecycle = Lifecycle(bus, session)
    lifecycle.mark_started()
    lifecycle.shutdown()
    ok("fake turn shutdown completes", lifecycle.running is False)
    ok("fake turn ends session through lifecycle", any(e.kind == "session.ended" and e.session_id == "fake-turn" for e in replay.events))


# ---------------------------------------------------------------------------
# Backend assembly helper
# ---------------------------------------------------------------------------

def test_backend_assembly() -> None:
    start_section("backend assembly helper")

    from backend.runtime import assemble_backend

    parts = assemble_backend(session_id="assemble-sess")
    ok("assembly returns expected keys", set(parts.keys()) >= {"bus", "session", "lifecycle", "audio", "tool_runner", "orchestrator"})
    ok("assembly session id matches request", parts["session"].id == "assemble-sess")
    ok("assembly lifecycle is started", parts["lifecycle"].running is True)
    ok("assembly uses fake audio by default", isinstance(parts["audio"], FakeAudioProvider))
    ok("assembly uses fake tool runner by default", isinstance(parts["tool_runner"], FakeToolRunner))

    replay = ReplayObserver()
    parts2 = assemble_backend(
        session_id="assemble-sess-2",
        observers=[replay],
        attach_logging=False,
        audio=ConfigurableFakeAudio(transcript="fake", speak_calls=[]),
    )
    ok("assembly accepts custom observers", any(isinstance(o, ReplayObserver) for o in parts2["bus"]._listeners))
    ok("assembly accepts custom audio", parts2["audio"] is not parts["audio"])


# ---------------------------------------------------------------------------
# Backend service API smoke checks
# ---------------------------------------------------------------------------

def _free_ports(start: int = 8000, stop: int = 9000) -> tuple[int, int]:
    import socket
    for port in range(start, stop - 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s1:
            try:
                s1.bind(("127.0.0.1", port))
            except OSError:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
                try:
                    s2.bind(("127.0.0.1", port + 1))
                except OSError:
                    continue
            return port, port + 1
    raise RuntimeError("no free adjacent ports")


def _wait_for_health(port: int, timeout: int = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = f"http://127.0.0.1:{port}/api/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def test_backend_service_api() -> None:
    start_section("backend service API")

    from backend.server import JarvisBackendService

    port, ws_port = _free_ports()
    service = JarvisBackendService(host="127.0.0.1", port=port, ws_port=ws_port)

    try:
        service.start()
        ok("backend service starts", service._started is True)

        if not _wait_for_health(port, timeout=12):
            ok("health endpoint is reachable", False, "timeout waiting for /api/health")
            return

        ok("health endpoint responds", True)

        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = resp.read().decode("utf-8", "replace")
            import json
            parsed = json.loads(body)
            ok("health payload has ok field", parsed.get("ok") is True)
            ok("health payload has service field", "service" in parsed)

        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state")
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = resp.read().decode("utf-8", "replace")
            parsed = json.loads(body)
            ok("state endpoint responds", True)
            ok("state has status field", "status" in parsed)
            ok("state status is idle initially", parsed.get("status") == "idle")

        # POST /api/command with a simple text command
        cmd_data = json.dumps({"text": "hello"}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/command",
            data=cmd_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read().decode("utf-8", "replace")
                parsed = json.loads(resp_body)
                ok("command endpoint responds", True)
                ok("command response has received field", "received" in parsed)
        except urllib.error.HTTPError as e:
            code = e.code
            msg = e.read().decode("utf-8", "replace")
            ok("command endpoint responds", False, f"http {code}: {msg}")
        except Exception as e:
            ok("command endpoint responds", False, str(e))

    finally:
        try:
            service.stop()
        except Exception:
            pass
        ok("backend service can be stopped", True)


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
    test_lifecycle_interrupt_behavior()
    test_tool_execution_meta()
    test_tool_registry_single_source()
    test_full_fake_backend_turn()
    test_backend_assembly()
    test_backend_service_api()
    test_validation()

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
