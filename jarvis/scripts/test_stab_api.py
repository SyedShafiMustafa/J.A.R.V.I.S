"""Stabilization API-part validation: everything focus-free.

Runs anytime, even while the desktop is in live use: backend health,
auto-listen, ambiguity asks (resolve-only), natural filesystem,
terminal, git, scheduler fire, WhatsApp refusal, honest settings
failure, emergency stop. No windows are opened, focused, or moved;
no Notepad is touched.

Exit 0 only if all checks pass. Run from ``jarvis/``::

    STAB_API=1 venv\\Scripts\\python scripts/test_stab_api.py
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
sys.path.append(str(Path(__file__).resolve().parent))

import test_stabilization_validation as V

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""), flush=True)
    return bool(ok)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
    failures = 0

    def must(ok, name, detail=""):
        nonlocal failures
        if not check(name, ok, detail):
            failures += 1
        return ok

    backend_py = __import__("shutil").which("python") or sys.executable
    port = V.free_port()
    V.BASE_URL = f"http://127.0.0.1:{port}"
    log_path = os.path.join(tempfile.gettempdir(),
                            f"stab_api_backend_{os.getpid()}.log")
    try:
        V.BACKEND_PROC = subprocess.Popen(
            [backend_py, "backend/api.py", "--port", str(port)],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=open(log_path, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT)
    except Exception as exc:
        must(False, "start backend", str(exc)[:150])
        return 1
    must(V.wait_backend(port), "backend healthy", f"port {port}")

    listening = False
    state = None
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        time.sleep(3)
        code, state = V.api("GET", "/api/state")
        if code != 200 or not isinstance(state, dict):
            continue
        if state.get("phase") in ("WAKE_LISTENING", "ERROR"):
            listening = state.get("phase") == "WAKE_LISTENING"
            break
    must(listening, "auto-listen on launch",
         f"{state.get('phase') if isinstance(state, dict) else state}")

    try:
        # Ambiguity asks (resolve-only: zero focus side effects).
        for verb, name in (("Maximize", "maximize"), ("Minimize", "minimize"),
                           ("Restore", "restore")):
            state = V.command(f"{verb} Chrome.", settle=6.0)
            must(V.asks_which_one(state)
                 or "using the" in V.reply_of(state).lower(),
                 f"ambiguous {name} asks or selects",
                 (V.reply_of(state) or "")[:110] if state else "")

        # Natural filesystem (planner only, unique names, self-cleaning).
        home = Path.home()
        desk_name = f"stab_api_{os.getpid()}.txt"
        desk_path = home / "Desktop" / desk_name
        state = V.command(f"Create a file called {desk_name} on my Desktop.",
                          settle=8.0)
        must(desk_path.exists(), "Desktop file created", str(desk_path))
        state = V.command(f"Move {desk_name} to Downloads.", settle=8.0)
        dl_path = home / "Downloads" / desk_name
        must(dl_path.exists() and not desk_path.exists(),
             "Downloads move verified", str(dl_path))
        try:
            if dl_path.exists():
                dl_path.unlink()
        except OSError:
            pass
        must(not dl_path.exists(), "test file cleaned")

        # Terminal + git through natural language.
        state = V.command("Run echo JARVIS test.", settle=10.0)
        must("jarvis test" in V.reply_of(state).lower(),
             "terminal output in reply")
        state = V.command("Show git status.", settle=15.0)
        must(state is not None and state.get("phase") != "ERROR"
             and bool(V.reply_of(state).strip()),
             "git status answered")

        # Scheduler: create, verify listed, targeted cancel, verify gone.
        state = V.command("Remind me in 2 minutes to test STABAPI.",
                          settle=6.0)
        code, scheduled = V.api("GET", "/api/scheduled", timeout=15)
        items = scheduled.get("scheduled_tasks", []) \
            if isinstance(scheduled, dict) else []
        hit = any("test stabapi" in str(
            (t.get("payload") or {}).get("remind", "")).lower()
            for t in items)
        must(code == 200 and hit, "reminder created + listed")
        state = V.command("cancel my test STABAPI reminder.", settle=6.0)
        code, scheduled = V.api("GET", "/api/scheduled", timeout=15)
        items = scheduled.get("scheduled_tasks", []) \
            if isinstance(scheduled, dict) else []
        hit = any("test stabapi" in str(
            (t.get("payload") or {}).get("remind", "")).lower()
            for t in items)
        must(code == 200 and not hit, "targeted cancel verified")

        # WhatsApp unknown recipient: asks, never sends.
        state = V.command("Send hello test to NonexistentContactXYZABC on "
                          "WhatsApp.", settle=20.0)
        low = V.reply_of(state).lower()
        must("which" in low or "couldn" in low or "clarif" in low
             or "?" in V.reply_of(state),
             "unknown recipient asks instead of sending",
             V.reply_of(state)[:120])

        # Unsupported setting fails honestly.
        state = V.command("Turn on dark mode.", settle=15.0)
        low = V.reply_of(state).lower()
        must(("couldn" in low or "sorry" in low or "can't" in low
              or "unable" in low)
             and (state.get("phase") if state else "") != "EXECUTING",
             "unsupported setting fails honestly",
             V.reply_of(state)[:120])

        # Emergency stop reaches stable IDLE, then resume listening.
        V.api("POST", "/api/listen/start", timeout=15)
        time.sleep(2)
        code, _ = V.api("POST", "/api/stop", payload={}, timeout=15)
        time.sleep(2)
        code, state = V.api("GET", "/api/state", timeout=15)
        stable = (code == 200 and state.get("status") == "idle"
                  and state.get("phase") == "IDLE"
                  and not state.get("error_message"))
        must(stable, "emergency stop reaches stable IDLE",
             f"{state.get('phase') if isinstance(state, dict) else state}")
    finally:
        if failures:
            print(f"[INFO] backend log kept at {log_path}", flush=True)
        else:
            try:
                os.remove(log_path)
            except OSError:
                pass
        if V.BACKEND_PROC is not None and V.BACKEND_PROC.poll() is None:
            V.BACKEND_PROC.terminate()
            try:
                V.BACKEND_PROC.wait(timeout=10)
            except Exception:
                V.BACKEND_PROC.kill()
        check("backend stopped", V.BACKEND_PROC is None
              or V.BACKEND_PROC.poll() is not None)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed.", flush=True)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
