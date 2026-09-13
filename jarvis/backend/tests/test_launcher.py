import subprocess

import pytest

from launcher import LauncherLock, _terminate, wait_for_url


def test_launcher_lock_prevents_duplicate_instance(tmp_path):
    path = tmp_path / "launcher.lock"
    first = LauncherLock(path)
    second = LauncherLock(path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_wait_for_url_stops_when_backend_process_exits():
    process = subprocess.Popen(
        ["python", "-c", "raise SystemExit(3)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert wait_for_url("http://127.0.0.1:1/api/health", timeout_sec=2, process=process) is False
    process.wait(timeout=2)


def test_terminate_accepts_processes_not_started():
    _terminate(None)
