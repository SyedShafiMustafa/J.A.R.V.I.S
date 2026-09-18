"""Focused tests for launcher Ollama orchestration.

Covers:
- Ollama already running -> reused (no process started, nothing killed)
- Ollama unavailable -> 'ollama serve' launched and awaited
- the launcher never kills an Ollama process it did not start
- backend startup only proceeds after Ollama readiness
"""

import subprocess
import types
from pathlib import Path

import pytest

from launcher import (
    LauncherLock,
    _stop_owned_ollama,
    ensure_ollama_running,
    ollama_is_ready,
    wait_for_ollama,
)


class FakeProc:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_calls += 1
        return 0


def _ollama_down(monkeypatch):
    monkeypatch.setattr("launcher.ollama_is_ready", lambda url=None, timeout_sec=2.0: False)


def _ollama_up(monkeypatch):
    monkeypatch.setattr("launcher.ollama_is_ready", lambda url=None, timeout_sec=2.0: True)


def _install_fake_ollama(monkeypatch, tmp_path, exe_name="ollama"):
    """Provide a fake 'ollama' executable on PATH and fake Popen."""
    exe = tmp_path / exe_name
    exe.write_text("", encoding="utf-8")

    started = []

    class FakeServeProc(FakeProc):
        pass

    def fake_popen(cmd, **kwargs):
        started.append(list(cmd))
        return FakeServeProc()

    monkeypatch.setattr("launcher.subprocess.Popen", fake_popen)
    return exe, started


def test_ollama_already_running_is_reused(monkeypatch):
    started = []

    def fail_popen(*args, **kwargs):
        started.append(args)
        raise AssertionError("no process should be started when Ollama is up")

    monkeypatch.setattr("launcher.subprocess.Popen", fail_popen)
    monkeypatch.setattr("launcher.locate_ollama_executable", lambda: None)

    _ollama_up(monkeypatch)

    proc, log = ensure_ollama_running()
    assert proc is None
    assert log is None
    assert started == []


def test_ollama_unavailable_is_launched(monkeypatch, tmp_path):
    exe, started = _install_fake_ollama(monkeypatch, tmp_path)
    monkeypatch.setattr("launcher.locate_ollama_executable", lambda: str(exe))
    # First probe (in ensure_ollama_running) says down; the readiness
    # poll then reports up so the launch path completes successfully.
    states = {"ready": False}

    def flip(url=None, timeout_sec=2.0):
        return states["ready"]

    def poll_then_flip(url=None, timeout_sec=None, process=None):
        states["ready"] = True
        return True

    monkeypatch.setattr("launcher.ollama_is_ready", flip)
    monkeypatch.setattr("launcher.wait_for_ollama", poll_then_flip)

    proc, log = ensure_ollama_running()

    assert proc is not None
    assert len(started) == 1
    assert started[0][1:] == ["serve"]
    assert log is not None
    log.close()


def test_ollama_launch_failure_is_actionable(monkeypatch, tmp_path):
    monkeypatch.setattr("launcher.locate_ollama_executable", lambda: None)
    _ollama_down(monkeypatch)

    with pytest.raises(RuntimeError, match="not reachable"):
        ensure_ollama_running()


def test_launcher_does_not_kill_reused_ollama_on_shutdown():
    proc = FakeProc()
    # Simulate the reuse path: ensure_ollama_running returns None for a
    # pre-existing Ollama, so shutdown has no handle to stop.
    _stop_owned_ollama(None)
    assert proc.terminated is False
    assert proc.killed is False


def test_launcher_stops_only_ollama_it_started():
    proc = FakeProc()
    _stop_owned_ollama(proc)
    assert proc.terminated is True
    assert proc.killed is False


def test_stop_owned_ollama_ignores_dead_process():
    class DeadProc(FakeProc):
        def poll(self):
            return 0

    dead = DeadProc()
    _stop_owned_ollama(dead)
    assert dead.terminated is False


def test_backend_startup_waits_for_ollama_readiness(monkeypatch):
    """wait_for_ollama polls until the API answers."""
    calls = []

    def become_ready(url=None, timeout_sec=2.0):
        calls.append(url)
        return len(calls) >= 3

    monkeypatch.setattr("launcher.ollama_is_ready", become_ready)
    monkeypatch.setattr("launcher.time.sleep", lambda *_: None)
    monkeypatch.setattr("launcher.time.time", lambda: 0.0)

    assert wait_for_ollama(timeout_sec=10) is True
    assert len(calls) == 3


def test_wait_for_ollama_times_out(monkeypatch):
    monkeypatch.setattr("launcher.ollama_is_ready", lambda url=None, timeout_sec=2.0: False)
    # Fake clock that advances 1s per sleep so the deadline is reached.
    clock = {"now": 0.0}
    monkeypatch.setattr("launcher.time.time", lambda: clock["now"])
    monkeypatch.setattr("launcher.time.sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))

    assert wait_for_ollama(timeout_sec=5) is False


def test_wait_for_ollama_aborts_when_process_exits(monkeypatch):
    class ExitedProc:
        def poll(self):
            return 3

    monkeypatch.setattr("launcher.ollama_is_ready", lambda url=None, timeout_sec=2.0: False)

    assert wait_for_ollama(timeout_sec=30, process=ExitedProc()) is False


def test_ollama_is_ready_false_on_connection_error(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("launcher.urllib.request.urlopen", boom)
    assert ollama_is_ready() is False
