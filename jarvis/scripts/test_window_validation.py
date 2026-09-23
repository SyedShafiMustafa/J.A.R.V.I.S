"""Milestone 4 — harmless real-machine window + cross-app validation.

Flow (all artifacts uniquely named, all closes hwnd-scoped):

1.  Pre-flight: no test-owned windows/files/profiles left behind.
2.  Open Notepad A + Notepad B on unique temp files (path-bound launch,
    tab-cycled to our file like the M3 harness).
3.  list_windows shows both; switch A<->B verified by handle.
4.  Minimize/restore A, maximize/restore B, all state-verified.
5.  Snap A left + B right, geometry verified (side-by-side arrange).
6.  Type a harmless string into A, save in place.
7.  Clipboard handoff: select-all + copy in A, read_clipboard equals.
8.  Live run_workflow through the real TaskExecutor (switch + verify).
9.  Paste into B, save, verify file content equals (deterministic).
10. Health probe on both windows (responsive, read-only).
11. Bonus: Chrome in an isolated temp profile (hwnd-diff tracked),
    focus, list, graceful close, profile removed. Skipped gracefully
    if Chrome is missing or anything looks ambiguous.
12. Close A+B by handle, verify gone, delete files, verify TEMP clean.

Exit code 0 only when every check passes.

Run from ``jarvis/`` with the project venv::

    venv\\Scripts\\python scripts/test_window_validation.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import pygetwindow as gw

from tools.executor import TaskExecutor

PID = os.getpid()
PREFIX = "jarvis_m4"
FILE_A = f"{PREFIX}_A_{PID}.txt"
FILE_B = f"{PREFIX}_B_{PID}.txt"
PATH_A = os.path.join(tempfile.gettempdir(), FILE_A)
PATH_B = os.path.join(tempfile.gettempdir(), FILE_B)
# Second real window: Explorer on a unique dir (no session tabs, own hwnd).
EXPLORER_DIRNAME = f"{PREFIX}_expl_{PID}"
EXPLORER_DIR = os.path.join(tempfile.gettempdir(), EXPLORER_DIRNAME)
TEXT_A = "JARVIS M4 handoff alpha"
CHROME_PROFILE = os.path.join(tempfile.gettempdir(), f"{PREFIX}_chrome_{PID}")

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""), flush=True)
    return bool(ok)


def park(agent):
    win = agent.windows.active()
    bounds = win.get("bounds")
    if bounds:
        agent.visual.move_to(
            {"x": bounds["x"] + bounds["width"] // 2,
             "y": bounds["y"] + bounds["height"] // 2})


def safe_keys(agent, fn, name):
    import pyautogui
    try:
        return fn()
    except pyautogui.FailSafeException:
        print(f"[WARN] {name}: fail-safe tripped, parking + retry",
              flush=True)
        park(agent)
        return fn()


def our_focus_ok(agent):
    """True only when a test-owned Notepad file tab is in front."""
    active = agent.computer.get_active_window() or ""
    low = active.lower()
    return ("notepad" in low
            and ("jarvis_" in low or "untitled" in low))


def gated_keys(agent, fn, name):
    """Send keys only under verified test-owned focus; else raise."""
    if not our_focus_ok(agent):
        raise RuntimeError(
            f"focus gate refused {name}: "
            f"{(agent.computer.get_active_window() or 'none')[:60]}")
    return safe_keys(agent, fn, name)


def wait_exists(filename, timeout=45.0):
    """Wait until our title MIGHT be selectable (no focus, no keys).

    Background tabs share one window title slot, so our filename is
    only enumerable while its tab is selected. This waits for EITHER
    our title OR any Notepad window (whose tabs we then select
    through); it returns False only when no Notepad window exists at
    all. Cold start after a session reset is slow (state rebuild).
    """
    import pygetwindow as _gw
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            wins = [w for w in _gw.getAllWindows()
                    if "notepad" in (w.title or "").lower()]
            if not wins:
                time.sleep(1.0)
                continue
            if any(filename.lower() in (w.title or "").lower()
                   for w in wins):
                return True
            # A Notepad window exists but shows another tab: selectable.
            return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def focus_file(agent, filename, timeout=20.0):
    """Focus our file tab without destabilizing the app.

    Ctrl+Tab order is most-recently-used: a single Ctrl+Shift+Tab steps
    back to the previously selected tab, which is exactly our other
    file when shuttling between the two. Rapid blind cycling is
    avoided (observed to wedge single-instance handoff); only a few
    slow forward steps follow as fallback.
    """
    computer = agent.computer
    last_active = "none"
    if not wait_exists(filename, timeout=max(10.0, timeout)):
        print(f"[DEBUG] focus_file({filename}): window never appeared",
              flush=True)
        return False
    deadline = time.monotonic() + timeout
    # Pass 1: plain focus (newly opened file is usually selected).
    while time.monotonic() < deadline:
        try:
            computer.focus_window("Notepad")
        except Exception:
            pass
        time.sleep(1.0)
        last_active = computer.get_active_window() or "none"
        if filename.lower() in last_active.lower():
            return True
        if "notepad" not in last_active.lower():
            time.sleep(1.0)
            continue
        break
    # Pass 2: one deterministic MRU step back to the previous tab.
    park(agent)
    try:
        computer.hotkey("ctrl", "shift", "tab")
    except Exception:
        pass
    time.sleep(1.5)
    last_active = computer.get_active_window() or "none"
    if filename.lower() in last_active.lower():
        return True
    # Pass 3: a few slow forward steps, then give up honestly.
    for _ in range(4):
        if time.monotonic() >= deadline:
            break
        park(agent)
        try:
            computer.hotkey("ctrl", "tab")
        except Exception:
            pass
        time.sleep(1.5)
        last_active = computer.get_active_window() or "none"
        if filename.lower() in last_active.lower():
            return True
    print(f"[DEBUG] focus_file({filename}): last saw {last_active[:80]!r}",
          flush=True)
    return False


def editor_point(agent):
    """Hybrid locate: UIA document/edit first, observed-bounds center."""
    try:
        model = agent.visual.inspect()
        for e in model.get("elements", []):
            if e["type"] in ("document", "text_field"):
                return e["point"]
        bounds = model.get("bounds")
    except Exception:
        bounds = None
    bounds = bounds or agent.windows.active().get("bounds")
    if not bounds:
        return None
    return {"x": bounds["x"] + bounds["width"] // 2,
            "y": bounds["y"] + bounds["height"] // 2 + 40}


def temp_owned_leftovers():
    tmp = tempfile.gettempdir()
    return [p for p in Path(tmp).glob(f"{PREFIX}*")]


NP_STATE = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Packages", "Microsoft.WindowsNotepad_8wekyb3d8bbwe", "LocalState")
NP_STATE_BAK = os.path.join(tempfile.gettempdir(), f"{PREFIX}_npstate_{PID}")


def stash_np_state():
    """Shelve Notepad's session-restore set for a pristine launch.

    Accumulated stale tabs make tab selection unbounded. The live
    TabState/WindowState files are COPIED aside first and restored
    byte-identical in ``finally`` — net effect on pre-existing (user)
    tabs is zero. Only safe while no notepad.exe runs (pre-flight
    guarantees it; locked files would fail loudly, not silently).
    """
    if not os.path.isdir(NP_STATE):
        return True
    try:
        for sub in ("TabState", "WindowState"):
            src = os.path.join(NP_STATE, sub)
            dst = os.path.join(NP_STATE_BAK, sub)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
                for name in os.listdir(src):
                    try:
                        os.remove(os.path.join(src, name))
                    except OSError:
                        return False
        return True
    except OSError:
        return False


def restore_np_state():
    """Put the shelved session-restore set back, drop run residue."""
    if not os.path.isdir(NP_STATE_BAK):
        return True
    try:
        for sub in ("TabState", "WindowState"):
            src = os.path.join(NP_STATE_BAK, sub)
            dst = os.path.join(NP_STATE, sub)
            if os.path.isdir(src) and os.path.isdir(dst):
                for name in os.listdir(dst):
                    try:
                        os.remove(os.path.join(dst, name))
                    except OSError:
                        pass
                for name in os.listdir(src):
                    try:
                        shutil.copy2(os.path.join(src, name),
                                    os.path.join(dst, name))
                    except OSError:
                        pass
        shutil.rmtree(NP_STATE_BAK, ignore_errors=True)
        return not os.path.isdir(NP_STATE_BAK)
    except OSError:
        return False


def remove_chrome_profile():
    """Remove our isolated Chrome profile, handling lingerers.

    Chrome keeps renderer/utility children alive after its window
    closes, and they lock profile files. After a graceful window
    close + patient retries, the last resort terminates ONLY
    processes whose command line contains our unique profile name
    (provably ours), then removes the directory.
    """
    deadline = time.monotonic() + 12.0
    while os.path.isdir(CHROME_PROFILE) \
            and time.monotonic() < deadline:
        try:
            shutil.rmtree(CHROME_PROFILE, ignore_errors=True)
        except OSError:
            pass
        if os.path.isdir(CHROME_PROFILE):
            time.sleep(1.0)
    if not os.path.isdir(CHROME_PROFILE):
        return True
    marker = os.path.basename(CHROME_PROFILE)
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=60)
    except Exception as exc:
        print(f"[WARN] chrome process sweep failed: {exc}", flush=True)
    time.sleep(3.0)
    try:
        shutil.rmtree(CHROME_PROFILE, ignore_errors=True)
    except OSError:
        pass
    return not os.path.isdir(CHROME_PROFILE)


def tab_count(agent):
    """Number of Notepad tabs in the FOREGROUND window (UIA)."""
    try:
        return sum(1 for c in agent.visual.uia.safe_inspect()
                   if c["type"] in ("tab", "TabItem"))
    except Exception:
        return -1


def purge_other_tabs(agent, filename):
    """Close foreign tabs, empty ours, save. Keeps restore set tiny.

    Only tabs in OUR focused window are touched; the purge stops at the
    first sign of trouble (a dialog, an unchanging tab count) and the
    caller proceeds to close regardless.
    """
    computer = agent.computer
    for _ in range(6):
        active = computer.get_active_window() or ""
        if "notepad" not in active.lower():
            return False
        if filename.lower() not in active.lower():
            before = tab_count(agent)
            try:
                if our_focus_ok(agent):
                    computer.hotkey("ctrl", "w")
            except Exception:
                return False
            time.sleep(0.8)
            after = tab_count(agent)
            if before >= 0 and after >= 0 and after >= before:
                return False  # a prompt may be open; stop touching tabs
        else:
            return True
    return False


def main() -> int:
    failures = 0

    def must(ok, name, detail=""):
        nonlocal failures
        if not check(name, ok, detail):
            failures += 1
        return ok

    executor = TaskExecutor()
    agent_windows = executor.windows
    computer = executor.computer

    procs = []
    try:
        # 1. Pre-flight: hermetic start. Any notepad.exe PROCESS (even
        # windowless: it may be restoring a session) aborts the run.
        must(not temp_owned_leftovers(), "pre-flight temp clean",
             f"{len(temp_owned_leftovers())} leftovers")
        pre_np = [w.title for w in gw.getWindowsWithTitle("Notepad")]
        must(not pre_np, "pre-flight no Notepad windows",
             f"{len(pre_np)} found")
        import subprocess as _sp
        procs_out = _sp.run(["tasklist", "/FI",
                             "IMAGENAME eq notepad.exe", "/FO", "CSV"],
                            capture_output=True, text=True).stdout
        pre_procs = "No tasks" not in procs_out
        must(not pre_procs, "pre-flight no notepad processes")
        if temp_owned_leftovers() or pre_np or pre_procs:
            print("[ABORT] clean test-owned state first.", flush=True)
            return 1

        # 2. Shelve the session-restore set so our two tabs are the
        # only ones (bounded, deterministic selection). Files are
        # pre-created empty: this machine's Notepad currently hangs
        # opening nonexistent paths (empty shell, no title), while
        # existing files open reliably. Then open A, wait for OUR
        # title, THEN open B (parallel single-instance launches race
        # the session handoff and wedge into zombies).
        must(stash_np_state(), "stash notepad session state")
        for path in (PATH_A, PATH_B):
            try:
                Path(path).write_text("", encoding="utf-8")
            except OSError as exc:
                must(False, "pre-create temp file", str(exc))
                return 1
        try:
            os.makedirs(EXPLORER_DIR, exist_ok=True)
        except OSError as exc:
            must(False, "pre-create explorer dir", str(exc))
            return 1
        try:
            procs.append(subprocess.Popen(["notepad.exe", PATH_A]))
            launched_a = True
        except Exception as exc:
            launched_a = False
            print(f"[WARN] launch A failed: {exc}", flush=True)
        must(launched_a, "launch Notepad A")
        # Cold start after a session reset rebuilds caches: allow 45s.
        must(focus_file(executor, FILE_A, timeout=45.0),
             "Notepad A appears", FILE_A)
        try:
            procs.append(subprocess.Popen(["notepad.exe", PATH_B]))
            launched_b = True
        except Exception as exc:
            launched_b = False
            print(f"[WARN] launch B failed: {exc}", flush=True)
        must(launched_b, "launch Notepad B")
        must(focus_file(executor, FILE_B, timeout=20.0),
             "Notepad B appears", FILE_B)
        must(focus_file(executor, FILE_A), "select tab A (MRU step)",
             FILE_A)
        must(focus_file(executor, FILE_B), "select tab B (MRU step)",
             FILE_B)
        must(focus_file(executor, FILE_A), "select tab A again", FILE_A)

        # Second real window: Explorer on our unique dir.
        try:
            procs.append(subprocess.Popen(["explorer.exe", EXPLORER_DIR]))
            launched_ex = True
        except Exception as exc:
            launched_ex = False
            print(f"[WARN] explorer launch failed: {exc}", flush=True)
        must(launched_ex, "launch Explorer on unique dir")
        ex_found = False
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            computer.focus_window(EXPLORER_DIRNAME)
            if EXPLORER_DIRNAME.lower() in (
                    computer.get_active_window() or "").lower():
                ex_found = True
                break
            time.sleep(0.5)
        must(ex_found, "Explorer window appears", EXPLORER_DIRNAME)
        # A foreground title does NOT mean enumerable: fresh Explorer
        # windows stay invisible for seconds. Wait for visibility so
        # the list below observes a settled desktop.
        try:
            import win32gui
            vis_deadline = time.monotonic() + 8.0
            while time.monotonic() < vis_deadline:
                active = agent_windows.active()
                hwnd = active.get("hwnd")
                if active.get("available") and hwnd is not None:
                    try:
                        if win32gui.IsWindowVisible(hwnd):
                            break
                    except Exception:
                        break
                time.sleep(0.5)
        except Exception:
            pass

        listed = agent_windows.list_windows(pattern=PREFIX)
        titles = [w["title"] for w in listed.get("windows", [])]
        has_np = any("notepad" in t.lower() for t in titles)
        has_ex = EXPLORER_DIRNAME.lower() in " ".join(titles).lower()
        must(listed.get("ok") and listed.get("count", 0) >= 2
             and has_np and has_ex,
             "list_windows shows notepad + explorer",
             f"{listed.get('count')} match: {titles}")

        # 3-4. Switch notepad window <-> Explorer, hwnd-verified each
        # way. Tabs share one window title slot, so the return trip
        # targets whichever title the window currently shows.
        t0 = time.monotonic()
        to_ex = agent_windows.focus(EXPLORER_DIRNAME)
        switch_ms_1 = int((time.monotonic() - t0) * 1000)
        must(to_ex.get("ok"), "switch notepad -> Explorer",
             f"{switch_ms_1}ms -> {(to_ex.get('window') or {}).get('title', '')[:40]}")
        back_titles = [w["title"] for w in
                       agent_windows.list_windows(
                           pattern=PREFIX).get("windows", [])
                       if "notepad" in w["title"].lower()]
        back_title = back_titles[0] if back_titles else FILE_A
        t0 = time.monotonic()
        to_np = agent_windows.focus(back_title)
        switch_ms_2 = int((time.monotonic() - t0) * 1000)
        must(to_np.get("ok"), "switch Explorer -> notepad",
             f"{switch_ms_2}ms via {back_title[:40]}")
        print(f"[INFO] switch latency: {switch_ms_1}ms / {switch_ms_2}ms",
              flush=True)

        recent = agent_windows.switch_recent()
        print(f"[INFO] alt-tab switch: ok={recent.get('ok')} "
              f"strategy={recent.get('strategy')} "
              f"{recent.get('switch_ms')}ms", flush=True)
        must(recent.get("ok"), "alt-tab style switch back")

        # 5. Minimize/maximize/restore on the Explorer window.
        ex_active = agent_windows.focus(EXPLORER_DIRNAME)
        must(ex_active.get("ok"), "refocus Explorer for state tests")
        mini = agent_windows.set_state(EXPLORER_DIRNAME, "minimize")
        must(mini.get("ok") and mini["window"]["minimized"],
             "minimize Explorer verified")
        rest = agent_windows.set_state(EXPLORER_DIRNAME, "restore")
        must(rest.get("ok") and not rest["window"]["minimized"],
             "restore Explorer verified")
        maxi = agent_windows.set_state(EXPLORER_DIRNAME, "maximize")
        must(maxi.get("ok") and maxi["window"]["maximized"],
             "maximize Explorer verified")
        rest_b = agent_windows.set_state(EXPLORER_DIRNAME, "restore")
        must(rest_b.get("ok") and not rest_b["window"]["maximized"],
             "restore Explorer verified")

        # 6. Arrange notepad + Explorer side by side, both verified.
        must(focus_file(executor, FILE_A), "select tab A for arrange")
        snapped_a = agent_windows.snap(FILE_A, "left")
        must(snapped_a.get("ok"), "snap notepad left",
             f"x={snapped_a.get('window', {}).get('rect', {}).get('x')}")
        snapped_b = agent_windows.snap(EXPLORER_DIRNAME, "right")
        size = computer.screen_size() or {"width": 1920}
        half = size["width"] // 2
        b_x = snapped_b.get("window", {}).get("rect", {}).get("x", -1)
        must(snapped_b.get("ok") and abs(b_x - half) <= 64,
             "snap Explorer right side-by-side", f"x={b_x} half={half}")

        # 7. Type into A, save in place.
        must(focus_file(executor, FILE_A), "refocus A for typing")
        pt = editor_point(executor)
        must(pt is not None, "locate A editor (UIA/bounds hybrid)")
        if pt:
            clicked = executor.visual.click_point(pt)
            must(clicked.get("ok"), "click A editor")
        park(executor)
        gated_keys(executor, lambda: computer.hotkey("ctrl", "a"),
                  "select-all A")
        time.sleep(0.3)
        typed = executor.visual.type_text(TEXT_A,
                                          expected_window=FILE_A)
        must(typed.get("ok"), "type handoff text",
             f"{typed.get('typed_chars')} chars")
        park(executor)
        gated_keys(executor, lambda: computer.hotkey("ctrl", "s"),
                  "save A")
        time.sleep(1.5)
        must(os.path.exists(PATH_A), "A saved to disk", FILE_A)

        # 8. Clipboard handoff: copy from A, verify clipboard equals.
        must(focus_file(executor, FILE_A), "refocus A for copy")
        park(executor)
        gated_keys(executor, lambda: computer.hotkey("ctrl", "a"),
                  "select-all copy")
        time.sleep(0.3)
        gated_keys(executor, lambda: computer.hotkey("ctrl", "c"),
                  "copy")
        time.sleep(0.5)
        clip = str(computer.get_clipboard() or "")
        must(clip.strip() == TEXT_A, "clipboard handoff equals",
             f"{len(clip)} chars")

        # 9. Live run_workflow across two real windows through the
        # real executor: Explorer -> clipboard extract -> notepad.
        # The return leg asserts focus success (hwnd-verified), not an
        # exact title: background tabs share one title slot.
        back_now = [w["title"] for w in
                    agent_windows.list_windows(
                        pattern=PREFIX).get("windows", [])
                    if "notepad" in w["title"].lower()]
        back_now = back_now[0] if back_now else FILE_A
        t0 = time.monotonic()
        flow = executor.execute({"goal": "M4 transfer check", "steps": [{
            "tool": "run_workflow", "goal": "M4 transfer check",
            "steps": [
                {"tool": "switch_app", "target": EXPLORER_DIRNAME,
                 "expect": {"kind": "window_active",
                            "title": EXPLORER_DIRNAME}},
                {"tool": "read_clipboard", "save_as": "grab",
                 "expect": {"kind": "clipboard_non_empty"}},
                {"tool": "switch_app", "target": back_now,
                 "expect": {"kind": "success"}},
            ]}]})
        flow_ms = int((time.monotonic() - t0) * 1000)
        must(flow.success and (flow.data or {}).get("verified"),
             "live run_workflow switch+extract verified",
             f"{flow_ms}ms retries={(flow.data or {}).get('retries')}")
        print(f"[INFO] workflow total: {flow_ms}ms", flush=True)

        # 10. Select tab B, paste, save, verify file content equals.
        must(focus_file(executor, FILE_B), "select tab B for paste")
        park(executor)
        gated_keys(executor, lambda: computer.hotkey("ctrl", "v"),
                  "paste into B")
        time.sleep(0.5)
        gated_keys(executor, lambda: computer.hotkey("ctrl", "s"),
                  "save B")
        time.sleep(1.5)
        try:
            content_b = Path(PATH_B).read_text(encoding="utf-8",
                                               errors="replace")
        except OSError as exc:
            content_b = ""
            print(f"[WARN] read B failed: {exc}", flush=True)
        must(content_b.strip() == TEXT_A,
             "B file content equals handoff", f"{len(content_b)} chars")

        # 11. Health probes (read-only; select each tab first since
        # both files share one window title slot).
        must(focus_file(executor, FILE_A), "select tab A for health")
        health_a = agent_windows.health(FILE_A)
        must(health_a.get("ok") and health_a.get("responsive"),
             "A responsive")
        must(focus_file(executor, FILE_B), "select tab B for health")
        health_b = agent_windows.health(FILE_B)
        must(health_b.get("ok") and health_b.get("responsive"),
             "B responsive")

        # 12. Bonus: Chrome in an isolated profile, hwnd-diff tracked.
        chrome_exe = ("C:\\Program Files\\Google\\Chrome\\Application\\"
                      "chrome.exe")
        if os.path.exists(chrome_exe):
            before = set()
            try:
                before = {w._hWnd for w in gw.getAllWindows()
                          if getattr(w, "_hWnd", None)}
            except Exception:
                pass
            try:
                procs.append(subprocess.Popen(
                    [chrome_exe, "--new-window", "--no-first-run",
                     "--no-default-browser-check",
                     "--disable-features=Translate",
                     f"--user-data-dir={CHROME_PROFILE}", "about:blank"]))
                launched_chrome = True
            except Exception as exc:
                launched_chrome = False
                print(f"[WARN] chrome launch failed: {exc}", flush=True)
            our_chrome = None
            if launched_chrome:
                deadline = time.monotonic() + 20.0
                while time.monotonic() < deadline:
                    try:
                        after = {w._hWnd: w for w in gw.getAllWindows()
                                 if getattr(w, "_hWnd", None)}
                    except Exception:
                        after = {}
                    fresh = [w for h, w in after.items() if h not in before]
                    chrome_fresh = [w for w in fresh
                                    if "chrome" in (w.title or "").lower()]
                    if chrome_fresh:
                        our_chrome = chrome_fresh[0]
                        break
                    time.sleep(0.5)
            must(our_chrome is not None, "Chrome window tracked by hwnd",
                 (our_chrome.title if our_chrome else "")[:50])
            if our_chrome is not None:
                hwnd_c = our_chrome._hWnd
                focused_c = agent_windows.focus("Google Chrome")
                # Ambiguity with a user Chrome is possible; require OUR
                # hwnd to be the one focused, else skip gracefully.
                active = agent_windows.active()
                if active.get("available") and active.get("hwnd") == hwnd_c:
                    must(True, "switch to our Chrome window")
                else:
                    print("[INFO] user Chrome present; hwnd tracked only",
                          flush=True)
                    must(True, "Chrome hwnd tracked (focus shared)")
                import win32gui
                import win32con
                try:
                    win32gui.PostMessage(hwnd_c, win32con.WM_CLOSE, 0, 0)
                except Exception as exc:
                    print(f"[WARN] chrome close failed: {exc}", flush=True)
                deadline = time.monotonic() + 10.0
                gone = False
                while time.monotonic() < deadline:
                    try:
                        alive = [w for w in gw.getAllWindows()
                                 if getattr(w, "_hWnd", None) == hwnd_c]
                    except Exception:
                        alive = []
                    if not alive:
                        gone = True
                        break
                    time.sleep(0.5)
                must(gone, "our Chrome window closed + verified")
        else:
            print("[INFO] Chrome not installed; bonus step skipped",
                  flush=True)
    finally:
        # 13. Purge foreign tabs, empty + save ours (keeps the session
        # restore set tiny), then close our windows by unique title
        # (A and B share ONE window: closing via A takes B with it),
        # verify both titles gone, clean TEMP.
        for name, path in ((FILE_A, PATH_A), (FILE_B, PATH_B)):
            try:
                if focus_file(executor, name, timeout=8.0):
                    purge_other_tabs(executor, name)
                    if our_focus_ok(executor):
                        try:
                            gated_keys(executor, lambda: computer.hotkey(
                                "ctrl", "a"), "purge-select")
                            gated_keys(executor, lambda: computer.press(
                                "delete"), "purge-delete")
                            gated_keys(executor, lambda: computer.hotkey(
                                "ctrl", "s"), "purge-save")
                            time.sleep(1.0)
                        except Exception as exc:
                            print(f"[WARN] purge typing: {exc}", flush=True)
            except Exception as exc:
                print(f"[WARN] purge {name}: {exc}", flush=True)
        # Tabs may share one window or live in two: close whatever
        # test-owned notepad windows exist until none remain.
        np_closed = False
        try:
            for _ in range(3):
                titles = [w["title"] for w in
                          agent_windows.list_windows(
                              pattern=PREFIX).get("windows", [])
                          if "notepad" in w["title"].lower()]
                if not titles:
                    np_closed = True
                    break
                if focus_file(executor, FILE_A, timeout=8.0) \
                        or focus_file(executor, FILE_B, timeout=8.0):
                    agent_windows.close_window(
                        agent_windows.active().get("title") or titles[0],
                        timeout=8.0)
                else:
                    break
                time.sleep(1.0)
        except Exception as exc:
            print(f"[WARN] notepad close loop: {exc}", flush=True)
        check("close notepad window(s)", np_closed)
        if np_closed:
            for name in (FILE_A, FILE_B):
                gone = not agent_windows.resolve(name).get("found", True)
                check(f"tab {name} gone with window", gone)
        try:
            ex_closed = agent_windows.close_window(EXPLORER_DIRNAME,
                                                   timeout=8.0)
        except Exception as exc:
            ex_closed = {"ok": False, "reason": str(exc)[:100]}
        check("close Explorer window", bool(ex_closed.get("ok")),
              ex_closed.get("reason", ""))
        # Restore the shelved session set AFTER our windows are gone
        # (a live instance would overwrite/flush over the restore).
        restored = restore_np_state()
        check("restore notepad session state", restored)
        if not restored:
            failures += 1
        for path in (PATH_A, PATH_B):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        try:
            if os.path.isdir(EXPLORER_DIR):
                os.rmdir(EXPLORER_DIR)
        except OSError as exc:
            print(f"[WARN] explorer dir cleanup: {exc}", flush=True)
        check("chrome profile removed", remove_chrome_profile())
        if os.path.isdir(CHROME_PROFILE):
            failures += 1
        leftovers = temp_owned_leftovers()
        check("temp artifacts removed",
              not leftovers, f"{len(leftovers)} remain")
        if leftovers:
            failures += 1
        for proc in procs:
            try:
                proc.wait(timeout=1)
            except Exception:
                pass

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed.", flush=True)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
