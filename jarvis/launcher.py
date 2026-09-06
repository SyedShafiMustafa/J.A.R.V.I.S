"""
launcher.py

Starts the Jarvis backend API stub and the Vite UI dev server together.

This is the simple local entry point for development and demos.

Usage:
    python launcher.py

Behavior:
    - starts the backend API on a free port
    - starts the UI dev server on a free port
    - waits for both to become reachable
    - prints the UI URL and opens it in the default browser
    - shuts both processes down cleanly on Ctrl+C
"""

from __future__ import annotations

import os
import signal
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
UI_DIR = HERE / "ui"
BACKEND_API = HERE / "backend" / "api.py"


def find_free_port(start: int = 8000, stop: int = 9000) -> int:
    for port in range(start, stop):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("No free backend port found")


def wait_for_url(url: str, timeout_sec: int = 60) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            req = urllib_request(url)
            with urllib_request_urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def urllib_request(url: str):
    import urllib.request
    return urllib.request.Request(url)


def urllib_request_urlopen(req, timeout: int = 3):
    import urllib.request
    return urllib.request.urlopen(req, timeout=timeout)


def run() -> None:
    backend_port = find_free_port()
    ui_port = find_free_port(start=5173, stop=5250)

    npm = shutil.which("npm") or "npm"

    print(f"[backend] api port: {backend_port}")
    print(f"[ui] dev server port: {ui_port}")

    backend_proc = subprocess.Popen(
        [sys.executable, str(BACKEND_API), "--port", str(backend_port)],
        cwd=str(HERE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    ui_proc = subprocess.Popen(
        [npm, "run", "dev", "--", "--port", str(ui_port)],
        cwd=str(UI_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    backend_url = f"http://127.0.0.1:{backend_port}/health"
    ui_url = f"http://127.0.0.1:{ui_port}/"

    try:
        if not wait_for_url(backend_url, timeout_sec=45):
            print("[launcher] backend did not become ready")
            sys.exit(1)

        print(f"[launcher] backend ready: {backend_url}")

        if not wait_for_url(ui_url, timeout_sec=60):
            print("[launcher] ui did not become ready")
            sys.exit(1)

        print(f"[launcher] ui ready: {ui_url}")
        print("[launcher] opening browser...")

        if not webbrowser.open(ui_url):
            print(f"[launcher] could not auto-open browser; open manually: {ui_url}")

        print("[launcher] running. press Ctrl+C to stop.\n")

        # Keep the launcher alive while both processes run.
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[launcher] shutting down...")
    finally:
        for proc in (backend_proc, ui_proc):
            if proc.poll() is None:
                proc.terminate()
        for proc in (backend_proc, ui_proc):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        print("[launcher] stopped")


def main() -> None:
    run()


if __name__ == "__main__":
    main()
