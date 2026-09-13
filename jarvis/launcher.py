"""
launcher.py

Starts the Jarvis backend service and the Vite UI dev server together.

This is the simple local entry point for development and demos.

Usage:
    python launcher.py [--no-browser]

Behavior:
    - starts the backend service on a free port
    - starts the UI dev server on a free port, pointed at that backend
    - waits for both to become reachable (real HTTP status, not 200-masquerading-errors)
    - prints the UI URL and opens it in the default browser
    - streams child output to logs/backend.log and logs/ui.log so
      failures are diagnosable instead of swallowed
    - shuts both processes down cleanly on Ctrl+C
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
UI_DIR = HERE / "ui"
BACKEND_API = HERE / "backend" / "api.py"
LOGS_DIR = HERE / "logs"
LOCK_PATH = HERE / ".jarvis-launcher.lock"


class LauncherLock:
    """Hold an OS-level lock for the lifetime of the launcher process."""

    def __init__(self, path: Path = LOCK_PATH) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        self._handle.seek(0)
        self._handle.write(f"{os.getpid()}\n")
        self._handle.flush()
        try:
            if os.name == "nt":
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            self._handle.close()
            self._handle = None
            raise RuntimeError(
                "J.A.R.V.I.S. is already running. Stop the existing launcher "
                "before starting another instance."
            )

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def find_free_port(start: int = 8000, stop: int = 9000) -> int:
    for port in range(start, stop):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("No free backend port found")


def wait_for_url(
    url: str,
    timeout_sec: int = 60,
    process: subprocess.Popen | None = None,
) -> bool:
    """Wait until the URL returns an actual HTTP 200 with valid JSON."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            return False
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    resp.read()
                    return True
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
            pass
        except Exception:
            pass
        time.sleep(1)
    return False


def tail(path: Path, n: int = 30) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return "(no log output yet)"


def run(no_browser: bool = False) -> None:
    # Make launcher prints show up immediately even when redirected to a file.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

    lock = LauncherLock()
    try:
        lock.acquire()
    except RuntimeError as exc:
        print(f"[launcher] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

    backend_proc = None
    ui_proc = None
    backend_log = None
    ui_log = None
    try:
        LOGS_DIR.mkdir(exist_ok=True)
        backend_port = find_free_port()
        ui_port = find_free_port(start=5173, stop=5250)
        npm = shutil.which("npm") or "npm"

        print(f"[launcher] backend port: {backend_port}")
        print(f"[launcher] ui port: {ui_port}")

        backend_log = open(LOGS_DIR / "backend.log", "w", encoding="utf-8")
        ui_log = open(LOGS_DIR / "ui.log", "w", encoding="utf-8")
        env = dict(os.environ)
        env["VITE_BACKEND_PORT"] = str(backend_port)

        backend_proc = subprocess.Popen(
            [sys.executable, str(BACKEND_API), "--port", str(backend_port)],
            cwd=str(HERE),
            stdout=backend_log,
            stderr=subprocess.STDOUT,
            text=True,
        )

        backend_url = f"http://127.0.0.1:{backend_port}/api/health"
        ui_url = f"http://127.0.0.1:{ui_port}/"
        print("[launcher] waiting for backend...")
        if not wait_for_url(backend_url, timeout_sec=45, process=backend_proc):
            print("[launcher] ERROR: backend failed to become ready")
            print("[launcher] backend.log tail:\n" + tail(LOGS_DIR / "backend.log"))
            raise SystemExit(1)

        print(f"[launcher] backend ready: {backend_url}")
        print("[launcher] waiting for ui...")

        ui_proc = subprocess.Popen(
            [npm, "run", "dev", "--", "--port", str(ui_port)],
            cwd=str(UI_DIR),
            env=env,
            stdout=ui_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if not wait_for_url(ui_url, timeout_sec=60, process=ui_proc):
            print("[launcher] ERROR: frontend failed to become ready")
            print("[launcher] ui.log tail:\n" + tail(LOGS_DIR / "ui.log"))
            raise SystemExit(1)

        print(f"[launcher] ui ready: {ui_url}")
        print("[launcher] opening browser...")

        if not no_browser and not webbrowser.open(ui_url):
            print(f"[launcher] could not auto-open browser; open manually: {ui_url}")

        print("[launcher] running. press Ctrl+C to stop.")
        print(f"[launcher] logs: {LOGS_DIR / 'backend.log'}, {LOGS_DIR / 'ui.log'}\n")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[launcher] shutting down...")
    finally:
        _terminate(backend_proc, ui_proc)
        if backend_log is not None:
            backend_log.close()
        if ui_log is not None:
            ui_log.close()
        lock.release()
        print("[launcher] stopped")


def _terminate(*procs: subprocess.Popen | None) -> None:
    for proc in procs:
        if proc is not None and proc.poll() is None:
            proc.terminate()
    for proc in procs:
        if proc is None:
            continue
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def main() -> None:
    no_browser = "--no-browser" in sys.argv
    run(no_browser=no_browser)


if __name__ == "__main__":
    main()