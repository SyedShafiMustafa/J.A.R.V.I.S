"""Milestone 3 — safe real-machine visual validation (Notepad).

Harmless flow on the local Windows machine:

1.  Open Notepad.
2.  Screenshot (active window).
3.  Locate the text editor region (UIA structure first, OCR fallback).
4.  Click the editor.
5.  Type ``JARVIS visual test``.
6.  Save to a unique temp file (gives our window a unique title so only
    OUR window is ever closed) and verify the file + OCR text.
7.  Open the File menu, verify it opened, close it, verify it closed.
8.  Close OUR Notepad window (WM_CLOSE to our hwnd only), verify it is
    gone, delete the temp file.

Nothing destructive: no other window is touched, the temp file is
removed, and every step prints PASS/FAIL with timings. Exit code is 0
only when every check passes.

Run from ``jarvis/`` with the project venv::

    venv\\Scripts\\python scripts/test_visual_validation.py
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import pygetwindow as gw
import win32gui
import win32con

from tools.computer import ComputerController
from tools.visual import VisualAgent
from tools.vision import ScreenVision

TEXT = "JARVIS visual test"
FILENAME = f"jarvis_visual_test_{os.getpid()}.txt"
FILEPATH = os.path.join(tempfile.gettempdir(), FILENAME)

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""), flush=True)
    return bool(ok)


def park_mouse(agent):
    """Park the pointer at the active-window center.

    pyautogui aborts any call when the pointer sits in a screen corner
    (fail-safe). Parking before key-only sequences keeps validation
    alive no matter where earlier steps left the mouse.
    """
    win = agent.active_window()
    bounds = win.get("bounds")
    if not bounds:
        return
    agent.move_to({"x": bounds["x"] + bounds["width"] // 2,
                   "y": bounds["y"] + bounds["height"] // 2})


def with_park_retry(agent, fn, name="input"):
    """Run ``fn``; on a fail-safe trip, park and retry exactly once."""
    import pyautogui
    try:
        return fn()
    except pyautogui.FailSafeException:
        print(f"[WARN] {name}: fail-safe tripped, parking and retrying",
              flush=True)
        park_mouse(agent)
        return fn()


def main() -> int:
    computer = ComputerController()
    agent = VisualAgent(computer=computer, vision=ScreenVision())
    failures = 0

    def must(ok, name, detail=""):
        nonlocal failures
        if not check(name, ok, detail):
            failures += 1
        return ok

    our_hwnd = None
    try:
        # 0. Hermetic pre-flight: never run against a pre-existing
        # Notepad (we must not touch another window's state).
        preexisting = gw.getWindowsWithTitle("Notepad")
        must(not preexisting, "no pre-existing Notepad window",
             f"{len(preexisting)} found" if preexisting else "clean")
        if preexisting:
            print("[ABORT] close existing Notepad windows first.",
                  flush=True)
            return 1

        # 1-2. Open Notepad directly on our unique file. Binding the
        # path up front means Ctrl+S saves in place: no Save As dialog,
        # no dialog-focus races, and our window carries a unique title
        # from birth so cleanup can never touch another window.
        t0 = time.monotonic()
        try:
            subprocess.Popen(["notepad.exe", FILEPATH])
            launched = True
        except Exception as exc:
            launched = False
            print(f"[WARN] launch failed: {exc}", flush=True)
        must(launched, "open Notepad on unique file",
             f"{int((time.monotonic() - t0) * 1000)}ms")
        must(computer.wait_for_window("Notepad", timeout=15),
             "Notepad window appears")

        # Focus gate: never send a single keystroke unless OUR window
        # (unique filename in the title) is verifiably in front.
        # Win11 Notepad restores stale session tabs on launch, so our
        # file's tab may not be selected: cycle tabs (bounded) until
        # OUR title shows. Foreign tabs are never modified or closed.
        focused = False
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            computer.focus_window("Notepad")
            active = computer.get_active_window() or ""
            if FILENAME.lower() in active.lower():
                focused = True
                break
            park_mouse(agent)
            try:
                computer.hotkey("ctrl", "tab")
            except Exception:
                pass
            time.sleep(0.5)
        must(focused, "focus Notepad before acting",
             (computer.get_active_window() or "none")[:60])
        if not focused:
            return 1

        # 3. Screenshot the active window.
        shot = agent.capture(mode="active")
        must(shot.get("ok"), "screenshot active window",
             f"{shot.get('width')}x{shot.get('height')} "
             f"in {shot.get('screenshot_ms')}ms")

        # 4. Locate the editor: UIA Document/Edit first; otherwise the
        # center of OUR observed window bounds (derived from the live
        # observation, never a hard-coded screen coordinate).
        model = agent.inspect()
        editor = next(
            (e for e in model["elements"]
             if e["type"] in ("document", "text_field")),
            None,
        )
        fallback = None
        if editor is None:
            bounds = (model.get("bounds")
                      or agent.active_window().get("bounds"))
            if bounds:
                fallback = {
                    "label": "window-center fallback",
                    "point": {"x": bounds["x"] + bounds["width"] // 2,
                              "y": bounds["y"] + bounds["height"] // 2},
                }
        editor = editor or fallback
        must(editor is not None, "locate text editor region",
             f"{editor['label'][:40]}@{editor['point']}" if editor else "")

        # 5. Click editor, replace the whole buffer (a stale tab from
        # an earlier run may hold old test text), type the test line.
        if editor:
            clicked = agent.click_point(editor["point"])
            must(clicked.get("ok"), "click editor",
                 f"{clicked.get('action_ms')}ms")
        park_mouse(agent)
        with_park_retry(agent,
                        lambda: computer.hotkey("ctrl", "a"), "select-all")
        time.sleep(0.3)
        typed = agent.type_text(TEXT, expected_window=FILENAME)
        must(typed.get("ok"), "type test text",
             f"{typed.get('typed_chars')} chars")

        # 6. Save in place (path was bound at launch: no dialog).
        # Re-check focus first so keys can never land elsewhere.
        active = computer.get_active_window() or ""
        must(FILENAME.lower() in active.lower(), "focus held before save",
             active[:60])
        park_mouse(agent)
        with_park_retry(agent,
                        lambda: computer.hotkey("ctrl", "s"), "save-in-place")
        saved = False
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if os.path.exists(FILEPATH):
                saved = True
                break
            time.sleep(0.4)
        must(saved, "save unique temp file", FILEPATH)
        title = computer.get_active_window() or ""
        must(FILENAME.lower() in title.lower(),
             "window title shows our file", title[:60])

        # Remember OUR window handle so cleanup can never hit another app.
        wins = gw.getWindowsWithTitle(FILENAME)
        our_hwnd = wins[0]._hWnd if wins else None
        must(our_hwnd is not None, "resolve our window handle")

        # 7. Re-observe and verify the typed text is visible.
        verify = agent.verify({"kind": "text_visible", "text": TEXT},
                              timeout=10.0)
        evidence = verify.get("evidence", {}) if isinstance(verify, dict) else {}
        must(verify.get("verified"), "verify typed text visible",
             f"{verify.get('verify_ms')}ms match={evidence.get('match')} "
             f"chars={evidence.get('chars_observed')}")
        if not verify.get("verified"):
            # One-shot debug: what did OCR actually see?
            dbg = agent.inspect(include_uia=False)
            print(f"[DEBUG] observed text: {dbg.get('text', '')[:300]!r}",
                  flush=True)

        # 8-10. Open the File menu (locate+click, Alt fallback),
        # verify, close it, verify it's gone.
        file_target = agent.locate("the File menu")
        menu_opened = False
        if file_target.get("found"):
            click = agent.click_point(file_target["located"]["point"])
            time.sleep(0.8)
            menu = agent.verify({"kind": "text_visible", "text": "New"},
                                timeout=6.0)
            menu_opened = click.get("ok") and menu.get("verified")
        if not menu_opened:
            # Alternate strategy: the Alt key opens the menu bar.
            park_mouse(agent)
            with_park_retry(agent,
                            lambda: computer.press("alt"), "menu-alt")
            time.sleep(0.8)
            menu = agent.verify({"kind": "text_visible", "text": "New"},
                                timeout=6.0)
            menu_opened = menu.get("verified", False)
        must(menu_opened, "open File menu + verify it appeared")
        park_mouse(agent)
        with_park_retry(agent,
                        lambda: computer.press("escape"), "close-menu")
        time.sleep(0.8)
        gone = agent.verify({"kind": "text_absent", "text": "New window"},
                            timeout=6.0)
        # Fallback marker: the second menu row varies by Notepad version.
        if not gone.get("verified"):
            gone = agent.verify({"kind": "text_absent", "text": "Print"},
                                timeout=6.0)
        must(gone.get("verified"), "close File menu + verify it closed")

        # 11. Timings summary (observability).
        model2 = agent.inspect()
        timings = model2.get("timings", {})
        print(f"[INFO] timings screenshot={timings.get('screenshot_ms')}ms "
              f"uia={timings.get('uia_ms')}ms ocr={timings.get('ocr_ms')}ms "
              f"inspect={timings.get('inspect_ms')}ms", flush=True)
    finally:
        # 12-14. Close ONLY our window, verify, remove the temp file.
        if our_hwnd is not None:
            try:
                win32gui.PostMessage(our_hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception as exc:
                print(f"[WARN] close failed: {exc}", flush=True)
            deadline = time.monotonic() + 8.0
            closed = False
            while time.monotonic() < deadline:
                try:
                    if not gw.getWindowsWithTitle(FILENAME):
                        closed = True
                        break
                except Exception:
                    closed = True
                    break
                time.sleep(0.4)
            check("close Notepad + verify window gone", closed)
            if not closed:
                failures += 1
        if os.path.exists(FILEPATH):
            try:
                os.remove(FILEPATH)
            except OSError:
                pass
        removed = not os.path.exists(FILEPATH)
        check("remove temp file", removed)
        if not removed:
            failures += 1

    print(f"\n{len(results) - failures}/{len(results)} checks passed.",
          flush=True)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
