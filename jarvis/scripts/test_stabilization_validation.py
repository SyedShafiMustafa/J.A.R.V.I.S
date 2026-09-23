"""Stabilization + action-first real-machine validation (§17 battery).

Drives the REAL backend HTTP API the way a user does. Every imperative
request must EXECUTE (never answer with instructions); every check
verifies actual machine state afterwards:

- backend healthy + auto-listen on launch
- "Open Chrome." executes (fast path)
- "Maximize/Minimize/Restore Chrome." execute when unambiguous, else
  ask which window instead of guessing or explaining
- "Put Chrome on the left." (+ unique-Notepad layout) geometry-verified
- "Open/Close the File menu." observed open/close, never Explorer
- visual typing + verification in unique Notepad
- cross-app clipboard handoff A -> B, file-verified
- filesystem: Desktop create -> Downloads move -> verify -> cleanup
- terminal echo output in the reply
- git status reply
- 1-minute reminder fires, then gone from the schedule
- WhatsApp unknown recipient asks, never sends
- unsupported setting fails honestly (no hallucinated success)
- emergency stop reaches stable IDLE

The backend under test spawns here on a free port with the
user-launch interpreter, then stops. Exit 0 only if all checks pass.

Run from ``jarvis/`` with the project venv::

    venv\\Scripts\\python scripts/test_stabilization_validation.py
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
sys.path.append(str(Path(__file__).resolve().parent))

import pygetwindow as gw

from test_window_validation import (
    focus_file,
    stash_np_state,
    restore_np_state,
)

PID = os.getpid()
PREFIX = "jarvis_stab"
# STAB_PART=api runs only focus-free checks (safe anytime); gui runs
# only focus/input checks (needs an idle desktop); all runs everything.
PART = os.environ.get("STAB_PART", "all").lower()
if PART not in ("api", "gui", "all"):
    PART = "all"


def want(section):
    if PART == "all":
        return True
    if PART == "api":
        return section == "api"
    return section == "gui"
FILE_A = f"{PREFIX}_A_{PID}.txt"
FILE_B = f"{PREFIX}_B_{PID}.txt"
PATH_A = os.path.join(tempfile.gettempdir(), FILE_A)
PATH_B = os.path.join(tempfile.gettempdir(), FILE_B)
TEXT_A = "JARVIShandoff42"
DESK_FILE = f"{PREFIX}_test_{PID}.txt"

results: list[tuple[str, bool, str]] = []
BASE_URL = ""
BACKEND_PROC = None


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""), flush=True)
    return bool(ok)


def api(method, path, payload=None, timeout=30):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except ValueError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return -1, str(exc)


def free_port(start=8100):
    for port in range(start, 9000):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port")


def wait_backend(port, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health",
                    timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


BUSY_STATUSES = ("thinking", "executing")


def command(text, settle=4.0, timeout=180, retry_transient=True):
    """POST a natural command, wait for the turn to leave busy states."""
    code, _ = api("POST", "/api/command", {"text": text}, timeout=120)
    if code not in (200, 409):
        return None
    state = _await_settled(settle, timeout)
    # The local fallback brain flakes transiently (30s truncation,
    # malformed JSON): one retry on planner-transient replies is safe
    # here (every battery command is idempotent or deduped).
    # Normalized (curly quotes, case) so the match cannot silently miss.
    norm = reply_of(state).lower().replace("\u2019", "'") if state else ""
    # ONLY planner-shape failures retry: timeouts ("took too long")
    # have unknown outcomes (the action may have executed), and
    # "try again" matches benign text — retrying either can
    # double-execute non-idempotent tools (proven by a WhatsApp
    # re-send attempt during validation).
    transient = bool(re.search(
        r"couldn.t safely interpret|malformed", norm))
    if transient:
        print(f"[DEBUG] transient reply detected: {norm[:100]!r}",
              flush=True)
    if retry_transient and state is not None and transient:
        print("[RETRY] transient planner failure, retrying once...",
              flush=True)
        code, _ = api("POST", "/api/command", {"text": text}, timeout=120)
        if code in (200, 409):
            state = _await_settled(settle, timeout)
    return state


def _await_settled(settle, timeout):
    deadline = time.monotonic() + timeout
    state = None
    while time.monotonic() < deadline:
        time.sleep(2)
        code2, state = api("GET", "/api/state", timeout=15)
        if code2 != 200 or not isinstance(state, dict):
            continue
        if state.get("status") not in BUSY_STATUSES:
            break
    time.sleep(settle)
    code2, state = api("GET", "/api/state", timeout=15)
    if code2 != 200 or not isinstance(state, dict):
        return None
    reply = state.get("reply") or ""
    if reply:
        print(f"[REPLY] {str(reply)[:170]}", flush=True)
    return state


def reply_of(state):
    return (state.get("reply") or "") if state else ""


def asks_which_one(state):
    low = reply_of(state).lower()
    return ("which one" in low or "several" in low or "matches" in low
            or "which window" in low)


def explains_how(state):
    """True when the reply reads like instructions, not a result."""
    low = reply_of(state).lower()
    return bool(re.search(
        r"(to maximize|to open|to move|click the|press \w|here.?s how|"
        r"you can (also|open|press|click)|alternatively|"
        r"step \d|first,|then,)", low))


def chrome_windows():
    try:
        return [w for w in gw.getAllWindows()
                if "chrome" in (w.title or "").lower()
                and "opera" not in (w.title or "").lower()]
    except Exception:
        return []


def main() -> int:
    global BASE_URL, BACKEND_PROC
    failures = 0

    def must(ok, name, detail=""):
        nonlocal failures
        if not check(name, ok, detail):
            failures += 1
        return ok

    from tools.executor import TaskExecutor
    executor = TaskExecutor()
    computer = executor.computer
    windows = executor.windows

    backend_py = shutil.which("python") or sys.executable
    port = free_port()
    BASE_URL = f"http://127.0.0.1:{port}"
    log_path = os.path.join(tempfile.gettempdir(),
                            f"{PREFIX}_backend_{PID}.log")
    try:
        BACKEND_PROC = subprocess.Popen(
            [backend_py, "backend/api.py", "--port", str(port)],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=open(log_path, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT)
    except Exception as exc:
        must(False, "start backend", str(exc)[:150])
        return 1
    must(wait_backend(port), "backend healthy",
         f"port {port} via {os.path.basename(backend_py)}")

    listening = False
    state = None
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        time.sleep(3)
        code, state = api("GET", "/api/state")
        if code != 200 or not isinstance(state, dict):
            continue
        if state.get("phase") in ("WAKE_LISTENING", "ERROR"):
            listening = state.get("phase") == "WAKE_LISTENING"
            break
    must(listening, "auto-listen on launch",
         f"{state.get('phase') if isinstance(state, dict) else state}")

    procs = []
    did_gui = False
    try:
        if want("gui"):
            # --- Pre-flight: abort on foreign Notepads (focus
            # contamination) and stash the session-restore set
            # (pristine tab selection). Leftovers from dead runs would
            # poison existence checks below.
            pre_np = [w.title for w in gw.getWindowsWithTitle("Notepad")]
            must(not pre_np, "pre-flight no Notepad", f"{len(pre_np)} found")
            # Prior runs keep their backend log for diagnosis; sweep
            # those here (logs only — data files still abort below).
            for old_log in Path(tempfile.gettempdir()).glob(
                    "jarvis_stab_backend_*.log"):
                try:
                    os.remove(old_log)
                except OSError:
                    pass
            stale = [p for p in Path(tempfile.gettempdir()).glob(
                "jarvis_stab*") if "backend_" not in p.name]
            must(not stale, "pre-flight temp clean",
                 f"{len(stale)} leftovers: {[p.name for p in stale][:3]}")
            if pre_np or stale:
                print("[ABORT] clean test-owned state first.",
                      flush=True)
                return 1
            must(stash_np_state(), "stash session state")
            did_gui = True
            for path in (PATH_A, PATH_B):
                Path(path).write_text("", encoding="utf-8")
        else:
            print("[SKIP] GUI pre-flight (api part needs no windows)",
                  flush=True)

        # --- APPLICATION: "Open Chrome." executes (full runs only:
        # launching pops a window over live work) ---
        if PART != "all":
            print("[SKIP] open-Chrome launch (full runs only)", flush=True)
            state = None
        else:
            state = command("Open Chrome.", settle=8.0)
        if PART == "all":
            must(state is not None and state.get("phase") != "ERROR",
                 "open Chrome executes", f"phase={state.get('phase')}")
            must(not explains_how(state),
                 "open reply is a result, not a tutorial")
            wins = chrome_windows()
            must(bool(wins), "Chrome window present", f"{len(wins)} found")

        # --- WINDOW: maximize/minimize/restore (resolve-only: safe
        # anytime; identical titles act verified, different ones ask) ---
        # Identical titles resolve by attention and execute verified;
        # genuinely different titles must ask instead of guessing.
        def _acted_or_asked(state, check_fn, name):
            if asks_which_one(state):
                return must(True, name + " (asked)", "")
            reply = (state.get("reply") or "") if state else ""
            if "using the" in reply.lower() and check_fn():
                return must(True, name + " (selected+verified)", "")
            return must(False, name, reply[:120])

        state = command("Maximize Chrome.", settle=6.0)
        _acted_or_asked(
            state,
            lambda: any(getattr(w, "isMaximized", False)
                        for w in chrome_windows()),
            "maximize Chrome")
        must(not explains_how(state), "maximize reply is not a tutorial")

        state = command("Minimize Chrome.", settle=6.0)
        _acted_or_asked(
            state,
            lambda: any(getattr(w, "isMinimized", False)
                        for w in chrome_windows()),
            "minimize Chrome")

        state = command("Restore Chrome.", settle=6.0)
        _acted_or_asked(
            state,
            lambda: any(not getattr(w, "isMinimized", True)
                        and not getattr(w, "isMaximized", True)
                        for w in chrome_windows()),
            "restore Chrome")

        # --- WINDOW LAYOUT on the unique Notepad (deterministic title) ---
        procs.append(subprocess.Popen(["notepad.exe", PATH_A]))
        must(focus_file(executor, FILE_A, timeout=30.0),
             "focus unique Notepad A", FILE_A)
        state = command(f"Put {FILE_A} on the left.", settle=8.0)
        listed = windows.list_windows(pattern=FILE_A)
        rect = (listed.get("windows", [{}])[0].get("rect", {})
                if listed.get("ok") else {})
        must(listed.get("ok") and abs(rect.get("x", 9999)) <= 64,
             "layout executes, geometry verified", f"x={rect.get('x')}")

        # --- VISUAL: File menu open/verify/close/verify (natural) ---
        from tools.ui_automation import UIAutomation
        uia = UIAutomation()

        def menu_items():
            try:
                return {c["name"] for c in uia.safe_inspect()
                        if c["type"] in ("Menu", "MenuItem")}
            except Exception:
                return set()

        must(focus_file(executor, FILE_A, timeout=20.0),
             "refocus Notepad for menu test")
        explorers_before = {w._hWnd for w in gw.getAllWindows()
                            if "file explorer" in (w.title or "").lower()}
        before = menu_items()
        state = command("Open the File menu.", settle=4.0)
        after = menu_items()
        opened = bool(after - before) and "File" in (after | before)
        menu_opened = must(opened, "File menu opened + verified",
                            f"new={sorted(after - before)[:6]}")
        must(not explains_how(state), "menu reply is not a tutorial")
        explorers_after = {w._hWnd for w in gw.getAllWindows()
                           if "file explorer" in (w.title or "").lower()}
        must(not (explorers_after - explorers_before),
             "menu request launched no Explorer")
        if menu_opened:
            state = command("Close the File menu.", settle=6.0)
            must(not (menu_items() - before), "File menu closed + verified")
        else:
            print("[SKIP] menu close check (menu never opened)", flush=True)

        # --- VISUAL typing + verification in unique Notepad ---
        # Single-token text keeps planner type-text exact; the focused
        # file + a save make the outcome file-verifiable.
        must(focus_file(executor, FILE_A, timeout=20.0),
             "refocus Notepad for typing")
        state = command(f"Type {TEXT_A} into Notepad.", settle=8.0)
        must(focus_file(executor, FILE_A, timeout=20.0),
             "refocus Notepad for save")
        from test_window_validation import gated_keys, park as _park
        _park(executor)
        try:
            gated_keys(executor, lambda: computer.hotkey("ctrl", "s"),
                       "save typed file")
        except RuntimeError as exc:
            must(False, "save typed file", str(exc)[:100])
        time.sleep(1.5)
        # Planner keystrokes can land seconds after the turn replies
        # under load: poll the file briefly instead of one-shot reading.
        content_ok = False
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                if TEXT_A in Path(PATH_A).read_text(
                        encoding="utf-8", errors="replace"):
                    content_ok = True
                    break
            except OSError:
                pass
            time.sleep(1.0)
        must(content_ok, "typing executed, file verified")

        # --- CROSS-APP clipboard handoff A -> B (natural paste leg) ---
        procs.append(subprocess.Popen(["notepad.exe", PATH_B]))
        must(focus_file(executor, FILE_B, timeout=25.0),
             "focus unique Notepad B", FILE_B)
        # Source setup at tool level (deterministic); the paste leg
        # below is the natural-language assertion.
        from test_window_validation import editor_point
        must(focus_file(executor, FILE_A, timeout=20.0),
             "refocus A for copy setup")
        pt = editor_point(executor)
        if pt is not None:
            executor.visual.click_point(pt)
        _park(executor)
        try:
            gated_keys(executor, lambda: computer.hotkey("ctrl", "a"),
                       "select-all A setup")
            typed = executor.visual.type_text(TEXT_A,
                                              expected_window=FILE_A)
            must(typed.get("ok"), "type handoff source",
                 f"{typed.get('typed_chars')} chars")
            gated_keys(executor, lambda: computer.hotkey("ctrl", "s"),
                       "save A setup")
            time.sleep(1.0)
            gated_keys(executor, lambda: computer.hotkey("ctrl", "a"),
                       "select-all A")
            gated_keys(executor, lambda: computer.hotkey("ctrl", "c"),
                       "copy A")
        except RuntimeError as exc:
            must(False, "copy from A", str(exc)[:100])
        time.sleep(0.5)
        clip = str(computer.get_clipboard() or "")
        must(TEXT_A in clip, "clipboard holds handoff text",
             f"{len(clip)} chars")
        must(focus_file(executor, FILE_B, timeout=20.0),
             "refocus B for paste")
        state = command("Paste.", settle=8.0)
        _park(executor)
        try:
            gated_keys(executor, lambda: computer.hotkey("ctrl", "s"),
                       "save B")
        except RuntimeError as exc:
            must(False, "save B", str(exc)[:100])
        time.sleep(1.5)
        content_b = ""
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                content_b = Path(PATH_B).read_text(
                    encoding="utf-8", errors="replace")
                if TEXT_A in content_b:
                    break
            except OSError:
                pass
            time.sleep(1.0)
        must(TEXT_A in content_b, "handoff file content verified",
             f"{len(content_b)} chars")

        # --- FILESYSTEM: Desktop create -> Downloads move (natural) ---
        desk_name = DESK_FILE
        state = command(f"Create a file called {desk_name} on my Desktop.",
                        settle=8.0)
        home = Path.home()
        desk_path = home / "Desktop" / desk_name
        must(desk_path.exists(), "Desktop file created", str(desk_path))
        state = command(f"Move {desk_name} to Downloads.", settle=8.0)
        dl_path = home / "Downloads" / desk_name
        must(dl_path.exists() and not desk_path.exists(),
             "Downloads move verified", str(dl_path))
        try:
            if dl_path.exists():
                dl_path.unlink()
        except OSError:
            pass
        must(not dl_path.exists(), "test file cleaned from Downloads")

        # --- TERMINAL: echo output lands in the reply ---
        state = command("Run echo JARVIS test.", settle=10.0)
        must("jarvis test" in reply_of(state).lower(),
             "terminal output in reply")

        # --- GIT: status reply, no error ---
        state = command("Show git status.", settle=15.0)
        must(state is not None and state.get("phase") != "ERROR"
             and bool(reply_of(state).strip()),
             "git status answered")

        # --- SCHEDULER: 1-minute reminder fires, then gone ---
        state = command("Remind me in 1 minute to test JARVIS.",
                        settle=6.0)
        code, scheduled = api("GET", "/api/scheduled", timeout=15)
        items = scheduled.get("scheduled_tasks", []) \
            if isinstance(scheduled, dict) else []
        hit = any("test jarvis" in str(
            (t.get("payload") or {}).get("remind", "")).lower()
            for t in items)
        must(code == 200 and hit, "1-minute reminder scheduled")
        print("[INFO] waiting ~75s for the reminder to fire...", flush=True)
        time.sleep(75)
        code, scheduled = api("GET", "/api/scheduled", timeout=15)
        items = scheduled.get("scheduled_tasks", []) \
            if isinstance(scheduled, dict) else []
        hit = any("test jarvis" in str(
            (t.get("payload") or {}).get("remind", "")).lower()
            for t in items)
        must(code == 200 and not hit, "reminder fired and left schedule")

        # --- WHATSAPP: unknown recipient asks, never sends ---
        state = command("Send hello test to NonexistentContactXYZABC on "
                        "WhatsApp.", settle=20.0)
        low = reply_of(state).lower()
        must(("which" in low or "couldn" in low or "clarif" in low
              or "?" in reply_of(state)),
             "unknown recipient asks instead of sending",
             reply_of(state)[:120])

        # --- SETTINGS: unsupported reversibly fails honestly ---
        state = command("Turn on dark mode.", settle=15.0)
        low = reply_of(state).lower()
        must(("couldn" in low or "sorry" in low or "can't" in low
              or "unable" in low)
             and state.get("phase") != "EXECUTING",
             "unsupported setting fails honestly, no fake success",
             reply_of(state)[:120])

        # --- EMERGENCY: stop reaches stable IDLE ---
        # NOTE: /api/stop requires a JSON body (strict content-type).
        api("POST", "/api/listen/start", timeout=15)
        time.sleep(2)
        code, _ = api("POST", "/api/stop", payload={}, timeout=15)
        time.sleep(2)
        code, state = api("GET", "/api/state", timeout=15)
        stable = (code == 200 and state.get("status") == "idle"
                  and state.get("phase") == "IDLE"
                  and not state.get("error_message"))
        must(stable, "emergency stop reaches stable IDLE",
             f"{state.get('phase') if isinstance(state, dict) else state}")
    finally:
        # --- Cleanup: windows, files, session, backend ---
        try:
            if focus_file(executor, FILE_A, timeout=8.0):
                try:
                    closed = windows.close_window(FILE_A, timeout=8.0)
                    check("close Notepad window",
                          bool(closed.get("ok")),
                          closed.get("reason", ""))
                except Exception as exc:
                    check("close Notepad window", False, str(exc)[:100])
        except Exception as exc:
            check("close Notepad window", False, str(exc)[:100])
        for path in (PATH_A, PATH_B):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        check("restore session state", restore_np_state())
        leftovers = [p for p in Path(tempfile.gettempdir()).glob("jarvis_stab*")
                     if "backend_" not in p.name]
        check("temp artifacts removed", not leftovers,
              f"{len(leftovers)} remain")
        if leftovers:
            failures += 1
        if failures:
            print(f"[INFO] backend log kept at {log_path}", flush=True)
        else:
            try:
                os.remove(log_path)
            except OSError:
                pass
        if BACKEND_PROC is not None and BACKEND_PROC.poll() is None:
            BACKEND_PROC.terminate()
            try:
                BACKEND_PROC.wait(timeout=10)
            except Exception:
                BACKEND_PROC.kill()
        check("backend stopped", BACKEND_PROC is None
              or BACKEND_PROC.poll() is not None)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed.", flush=True)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
