"""
tools/windows.py
----------------
Milestone 4 — active-window management and workspace control.

Reliable Windows desktop control built on the existing stack:

- ``ComputerController`` (tools/computer.py) for focus + hotkeys
- ``pygetwindow`` for enumeration, state and geometry
- Win32 (``win32gui``) for hwnd liveness, foreground fallback,
  hung-app probing and graceful close

Every action verifies the resulting window state and returns a
structured dict — never a bare claim. Ambiguous targets are refused
with candidates instead of blindly acting on the first match.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pyautogui
import pygetwindow as gw

from tools.computer import ComputerController

_log = logging.getLogger("jarvis.windows")

WINDOW_ACTIONS = ("minimize", "maximize", "restore")
RECT_TOLERANCE_PX = 16
SNAP_POS_TOLERANCE_PX = 32
SNAP_SIZE_TOLERANCE_PX = 64
STATE_TIMEOUT_S = 4.0
STATE_POLL_S = 0.25
FOCUS_SETTLE_S = 0.3
CLOSE_TIMEOUT_S = 8.0
HUNG_PROBE_TIMEOUT_MS = 1500
#: Fresh windows (Explorer navigating under load) exist titled but
#: invisible for seconds, and invisible windows never enumerate.
#: Pattern searches poll fresh snapshots this long before reporting
#: zero matches. Genuine misses pay this budget once per call.
ENUM_SETTLE_S = 4.0
ENUM_POLL_S = 0.5

#: Friendly names -> title fragments (substring, case-insensitive).
APP_ALIASES = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "edge",
    "microsoft edge": "edge",
    "firefox": "firefox",
    "notepad": "notepad",
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "code": "visual studio code",
    "explorer": "file explorer",
    "file explorer": "file explorer",
    "whatsapp": "whatsapp",
    "word": "word",
    "excel": "excel",
    "powerpoint": "powerpoint",
    "outlook": "outlook",
    "teams": "teams",
    "discord": "discord",
    "spotify": "spotify",
    "cmd": "command prompt",
    "command prompt": "command prompt",
    "powershell": "powershell",
    "terminal": "terminal",
}


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _fail(reason: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "reason": reason}
    out.update(extra)
    return out


def _win32() -> Any | None:
    try:
        import win32gui
        return win32gui
    except Exception:
        return None


def _is_alive(hwnd: int) -> bool:
    w32 = _win32()
    if w32 is None:
        return True  # cannot probe: keep legacy optimistic contract
    try:
        return bool(w32.IsWindow(hwnd))
    except Exception:
        return True


def work_area(computer) -> dict[str, int]:
    """Usable desktop rect (taskbar excluded), screen fallback.

    ``pyautogui.size()`` returns the full primary monitor including the
    taskbar; snapping against it parks windows underneath it. The Win32
    work area is preferred where available.
    """
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(rect), 0):
            return {"x": int(rect.left), "y": int(rect.top),
                    "width": int(rect.right - rect.left),
                    "height": int(rect.bottom - rect.top)}
    except Exception:
        pass
    size = computer.screen_size() or {"width": 1920, "height": 1080}
    return {"x": 0, "y": 0, "width": size["width"],
            "height": size["height"]}


LAYOUTS = ("left", "right", "top", "bottom", "maximize")


def layout_rect(layout: str, work: dict[str, int]) -> dict[str, int] | None:
    """Target rectangle for a named half (or full work area)."""
    if layout not in LAYOUTS:
        return None
    if layout == "maximize":
        return dict(work)
    if layout == "left":
        half = work["width"] // 2
        return {"x": work["x"], "y": work["y"],
                "width": half, "height": work["height"]}
    if layout == "right":
        half = work["width"] // 2
        return {"x": work["x"] + half, "y": work["y"],
                "width": work["width"] - half, "height": work["height"]}
    if layout == "top":
        half = work["height"] // 2
        return {"x": work["x"], "y": work["y"],
                "width": work["width"], "height": half}
    half = work["height"] // 2
    return {"x": work["x"], "y": work["y"] + half,
            "width": work["width"], "height": work["height"] - half}


def rects_match(actual: dict[str, int], want: dict[str, int],
                pos_tol: int = SNAP_POS_TOLERANCE_PX,
                size_tol: int = SNAP_SIZE_TOLERANCE_PX) -> bool:
    """Position must match; size is tolerant (DWM gaps/frames vary)."""
    return (abs(actual.get("x", -9999) - want["x"]) <= pos_tol
            and abs(actual.get("y", -9999) - want["y"]) <= pos_tol
            and abs(actual.get("width", -9999) - want["width"]) <= size_tol
            and abs(actual.get("height", -9999) - want["height"])
            <= size_tol)


def _is_hung(hwnd: int) -> bool | None:
    """True/False via user32.IsHungAppWindow; None when unprobeable.

    ``IsHungAppWindow`` is authoritative (5s kernel threshold) and has
    no message-result ambiguity, unlike ``SendMessageTimeout`` whose
    ``WM_NULL`` result is 0 even when healthy.
    """
    try:
        import ctypes
        from ctypes import wintypes
        probe = ctypes.windll.user32.IsHungAppWindow
        probe.argtypes = [wintypes.HWND]
        probe.restype = wintypes.BOOL
        return bool(probe(hwnd))
    except Exception:
        return None


class WindowManager:
    """Detect, identify, focus, arrange and health-check windows."""

    def __init__(self, computer: ComputerController | None = None,
                 desktop: Any | None = None) -> None:
        self.computer = computer or ComputerController()
        self.desktop = desktop  # DesktopController, wired by the executor
        self.last_error: str | None = None
        self._focus_history: list[int] = []

    # ------------------------------------------------------------------
    # OBSERVE — enumerate + describe
    # ------------------------------------------------------------------

    def list_windows(self, pattern: str | None = None,
                     limit: int = 30) -> dict[str, Any]:
        """All visible titled windows, newest-observed order not promised.

        Stale (dead-hwnd) entries are filtered. Each entry carries hwnd,
        title, app hint, state flags and geometry — everything targeting
        and verification need, nothing more.
        """
        t0 = time.monotonic()
        try:
            raw = gw.getAllWindows()
        except Exception as exc:
            return _fail("enumerate_failed", error=str(exc)[:200],
                         list_ms=_ms(t0))
        needle = (pattern or "").strip().lower()
        windows = self._collect(raw, needle, limit)
        if needle and not windows:
            # Fresh windows exist titled but invisible for up to ~1s
            # (Explorer navigating): poll fresh snapshots until they
            # enumerate or the settle budget runs out.
            deadline = time.monotonic() + ENUM_SETTLE_S
            while not windows and time.monotonic() < deadline:
                time.sleep(ENUM_POLL_S)
                try:
                    raw = gw.getAllWindows()
                except Exception as exc:
                    return _fail("enumerate_failed",
                                 error=str(exc)[:200],
                                 list_ms=_ms(t0))
                windows = self._collect(raw, needle, limit)
        return {"ok": True, "windows": windows, "count": len(windows),
                "list_ms": _ms(t0)}

    def _collect(self, raw: list, needle: str,
                 limit: int) -> list[dict[str, Any]]:
        windows: list[dict[str, Any]] = []
        for w in raw:
            try:
                title = (w.title or "").strip()
                if not title:
                    continue
                hwnd = w._hWnd
            except Exception:
                continue
            if not _is_alive(hwnd):
                continue
            if needle and needle not in title.lower():
                continue
            windows.append(self.describe_window(w))
            if len(windows) >= max(1, limit):
                break
        return windows

    def describe_window(self, w: Any) -> dict[str, Any]:
        try:
            left, top, width, height = w.left, w.top, w.width, w.height
        except Exception:
            left, top, width, height = 0, 0, 0, 0
        try:
            minimized = bool(w.isMinimized)
            maximized = bool(w.isMaximized)
        except Exception:
            minimized, maximized = False, False
        try:
            active = w.isActive
        except Exception:
            active = False
        title = ""
        try:
            title = (w.title or "").strip()
        except Exception:
            pass
        return {
            "hwnd": getattr(w, "_hWnd", None),
            "title": title,
            "app": self.guess_app(title),
            "visible": True,
            "minimized": minimized,
            "maximized": maximized,
            "active": bool(active),
            "rect": {"x": left, "y": top,
                     "width": width, "height": height},
        }

    @staticmethod
    def guess_app(title: str) -> str:
        low = (title or "").lower()
        for alias, fragment in APP_ALIASES.items():
            if fragment in low:
                # Canonical display name for the best-known apps.
                return {
                    "chrome": "Chrome",
                    "visual studio code": "VS Code",
                    "notepad": "Notepad",
                    "file explorer": "Explorer",
                    "whatsapp": "WhatsApp",
                    "edge": "Edge",
                }.get(fragment, alias.title())
        # "Untitled - Notepad" -> "Notepad"; otherwise the full title.
        return title.rsplit(" - ", 1)[-1].strip() or title

    def active(self) -> dict[str, Any]:
        """The foreground window, verified, or ``available: False``."""
        try:
            win = gw.getActiveWindow()
        except Exception as exc:
            return {"available": False, "error": str(exc)[:200]}
        if win is None:
            return {"available": False}
        desc = self.describe_window(win)
        desc["available"] = True
        return desc

    # ------------------------------------------------------------------
    # TARGETING — natural language -> one window or an honest refusal
    # ------------------------------------------------------------------

    def resolve(self, target: str) -> dict[str, Any]:
        """Resolve ``"Switch to Chrome"``-style targets.

        Exact title match wins outright. One substring match wins.
        Several matches -> ``ambiguous`` with candidates (never the
        first match blindly). No match -> ``not_found``. Dead hwnds are
        filtered before matching so stale windows cannot win.
        """
        t0 = time.monotonic()
        if not isinstance(target, str) or not target.strip():
            return _fail("invalid_target", target=target,
                         resolve_ms=_ms(t0))
        listing = self.list_windows()
        if not listing.get("ok"):
            return _fail("observe_failed", target=target.strip(),
                         resolve_ms=_ms(t0))
        query = target.strip().lower()
        fragment = APP_ALIASES.get(query, query)

        def _match(windows: list) -> tuple[list, list]:
            exact = [w for w in windows if w["title"].lower() == query]
            if len(exact) == 1:
                return exact, exact
            matches = [w for w in windows
                       if fragment in w["title"].lower()]
            if not matches and fragment != query:
                # Alias expansion can over-narrow ("code" is inside many
                # titles): retry on the raw query before giving up.
                matches = [w for w in windows
                           if query in w["title"].lower()]
            return exact, matches

        exact, matches = _match(listing.get("windows", []))
        if not exact and not matches:
            # Same visibility race one layer up: a fresh snapshot (the
            # settle poll inside list_windows covers it).
            time.sleep(ENUM_POLL_S)
            exact, matches = _match(
                self.list_windows().get("windows", []))
        if len(exact) == 1:
            return {"ok": True, "found": True, "ambiguous": False,
                    "window": exact[0], "candidates": [exact[0]],
                    "resolve_ms": _ms(t0)}
        if not matches:
            self.last_error = "window not found"
            return {"ok": True, "found": False, "ambiguous": False,
                    "reason": "not_found", "target": target.strip(),
                    "candidates": [], "resolve_ms": _ms(t0)}
        if len(matches) == 1:
            self.last_error = None
            return {"ok": True, "found": True, "ambiguous": False,
                    "window": matches[0], "candidates": matches,
                    "resolve_ms": _ms(t0)}
        titles = {w["title"].lower() for w in matches}
        if len(titles) == 1:
            # Identical titles: asking "which one" is unanswerable, so
            # resolve by attention instead — the foreground window if it
            # is one of them, else the most recently focused one. The
            # choice is reported (selection) and verified downstream.
            hwnds = [w["hwnd"] for w in matches]
            foreground = self._foreground_hwnd()
            if foreground in hwnds:
                pick = next(w for w in matches
                            if w["hwnd"] == foreground)
                self.last_error = None
                return {"ok": True, "found": True, "ambiguous": False,
                        "window": pick, "candidates": matches,
                        "selection": "active_window",
                        "resolve_ms": _ms(t0)}
            for recent in reversed(self._focus_history):
                if recent in hwnds:
                    pick = next(w for w in matches
                                if w["hwnd"] == recent)
                    self.last_error = None
                    return {"ok": True, "found": True, "ambiguous": False,
                            "window": pick, "candidates": matches,
                            "selection": "recently_used",
                            "resolve_ms": _ms(t0)}
        self.last_error = "ambiguous window"
        return {"ok": True, "found": False, "ambiguous": True,
                "reason": "ambiguous", "target": target.strip(),
                "candidates": matches[:5], "resolve_ms": _ms(t0)}

    # ------------------------------------------------------------------
    # ACT — focus / switch / state / geometry, all verified
    # ------------------------------------------------------------------

    def focus(self, target: str) -> dict[str, Any]:
        """Focus a resolved window and verify the foreground changed."""
        t0 = time.monotonic()
        resolved = self.resolve(target)
        if not resolved.get("ok") or not resolved.get("found"):
            return {**resolved, "ok": False,
                    "reason": resolved.get("reason", "target not found"),
                    "focus_ms": _ms(t0)}
        window = resolved["window"]
        # Pin the handle: focusing by title can land on a same-titled
        # sibling (two Chromes), then fail our own hwnd verification.
        focus_hwnd = getattr(self.computer, "focus_hwnd", None)
        focused = focus_hwnd(window["hwnd"]) if callable(focus_hwnd) \
            else False
        if not focused:
            # Fall back to the title path for controllers that only
            # implement the legacy call (fakes, exotic platforms).
            if not self.computer.focus_window(window["title"]):
                return _fail("focus_failed", target=target.strip(),
                             window=window, focus_ms=_ms(t0))
        # Verify by hwnd, not title: titles can repeat, handles cannot.
        confirmed = self._foreground_hwnd() == window["hwnd"]
        if confirmed:
            self._remember(window["hwnd"])
            return {"ok": True, "window": self.active(),
                    "resolved": window,
                    "selection": resolved.get("selection"),
                    "focus_ms": _ms(t0)}
        return _fail("focus_failed", target=target.strip(),
                     window=window, focus_ms=_ms(t0))

    def switch_recent(self) -> dict[str, Any]:
        """Alt+Tab-style switch to the previously active window.

        A real Alt+Tab chord first (verified); history fallback when the
        chord does not move focus (some shells swallow it).
        """
        t0 = time.monotonic()
        before = self._foreground_hwnd()
        try:
            pyautogui.keyDown("alt")
            pyautogui.press("tab")
            pyautogui.keyUp("alt")
            time.sleep(0.5)
        except Exception as exc:
            try:
                pyautogui.keyUp("alt")
            except Exception:
                pass
            return _fail("switch_failed", error=str(exc)[:200],
                         switch_ms=_ms(t0))
        after = self._foreground_hwnd()
        if after is not None and after != before:
            self._remember(after)
            return {"ok": True, "window": self.active(),
                    "strategy": "alt_tab", "switch_ms": _ms(t0)}
        # Fallback: most recent tracked hwnd that is alive and different.
        for hwnd in reversed(self._focus_history):
            if hwnd != before and _is_alive(hwnd):
                w32 = _win32()
                if w32 is None:
                    continue
                try:
                    w32.SetForegroundWindow(hwnd)
                    time.sleep(FOCUS_SETTLE_S)
                except Exception:
                    continue
                if self._foreground_hwnd() == hwnd:
                    self._remember(hwnd)
                    return {"ok": True, "window": self.active(),
                            "strategy": "history", "switch_ms": _ms(t0)}
        return _fail("switch_failed", detail="focus did not move",
                     switch_ms=_ms(t0))

    def set_state(self, target: str, state: str) -> dict[str, Any]:
        """Minimize / maximize / restore a window, state verified after."""
        t0 = time.monotonic()
        if state not in WINDOW_ACTIONS:
            return _fail("invalid_state", state=state,
                         state_ms=_ms(t0))
        resolved = self.resolve(target)
        if not resolved.get("ok") or not resolved.get("found"):
            return {**resolved, "ok": False,
                    "reason": resolved.get("reason", "target not found"),
                    "state_ms": _ms(t0)}
        window = resolved["window"]
        if not _is_alive(window["hwnd"]):
            return _fail("stale_window", target=target.strip(),
                         window=window, state_ms=_ms(t0))
        # Pin the handle, not the title: Chrome retitles per tab and a
        # title re-lookup can hand us a different window (or nothing).
        live = self._live(window["hwnd"])
        if live is None:
            return _fail("stale_window", target=target.strip(),
                         window=window, state_ms=_ms(t0))
        try:
            if state == "minimize":
                live.minimize()
            elif state == "maximize":
                live.maximize()
            else:
                live.restore()
        except Exception as exc:
            return _fail("state_failed", target=target.strip(),
                         error=str(exc)[:200], state_ms=_ms(t0))

        def _reached(desc):
            if state == "minimize":
                return desc["minimized"]
            if state == "maximize":
                return desc["maximized"]
            # Restore can cross a maximized transient on the way back;
            # poll until it settles instead of sampling once.
            return not desc["minimized"] and not desc["maximized"]

        after = self._poll_desc(window["hwnd"], _reached)
        if after is not None and _reached(after):
            return {"ok": True, "window": after, "state": state,
                    "state_ms": _ms(t0)}
        return _fail("state_not_reached", target=target.strip(),
                     state=state, window=after, state_ms=_ms(t0))

    def move_resize(self, target: str, x: int, y: int,
                    width: int, height: int) -> dict[str, Any]:
        """Move + resize a window, geometry verified within tolerance."""
        t0 = time.monotonic()
        try:
            x, y = int(x), int(y)
            width, height = int(width), int(height)
        except (TypeError, ValueError):
            return _fail("invalid_geometry",
                         geometry={"x": x, "y": y,
                                   "width": width, "height": height},
                         move_ms=_ms(t0))
        if (isinstance(x, bool) or x < 0 or y < 0
                or width < 1 or height < 1):
            return _fail("invalid_geometry",
                         geometry={"x": x, "y": y,
                                   "width": width, "height": height},
                         move_ms=_ms(t0))
        resolved = self.resolve(target)
        if not resolved.get("ok") or not resolved.get("found"):
            return {**resolved, "ok": False,
                    "reason": resolved.get("reason", "target not found"),
                    "move_ms": _ms(t0)}
        window = resolved["window"]
        live = self._live(window["hwnd"])
        if live is None:
            return _fail("stale_window", target=target.strip(),
                         move_ms=_ms(t0))
        try:
            try:
                live.restore()  # maximized windows ignore geometry calls
            except Exception:
                pass
            live.moveTo(x, y)
            live.resizeTo(width, height)
        except Exception as exc:
            return _fail("move_failed", target=target.strip(),
                         error=str(exc)[:200], move_ms=_ms(t0))

        def _positioned(desc):
            rect = desc["rect"]
            return (abs(rect["x"] - x) <= RECT_TOLERANCE_PX
                    and abs(rect["y"] - y) <= RECT_TOLERANCE_PX)

        after = self._poll_desc(window["hwnd"], _positioned)
        if after is None:
            return _fail("stale_window", target=target.strip(),
                         move_ms=_ms(t0))
        rect = after["rect"]
        # DWM clamps/resizes decorated frames: position match is the
        # binding check, size match is best-effort evidence.
        size_exact = (abs(rect["width"] - width) <= RECT_TOLERANCE_PX
                      and abs(rect["height"] - height) <= RECT_TOLERANCE_PX)
        if _positioned(after):
            return {"ok": True, "window": after,
                    "requested": {"x": x, "y": y,
                                  "width": width, "height": height},
                    "size_exact": bool(size_exact), "move_ms": _ms(t0)}
        return _fail("move_not_reached", target=target.strip(),
                     window=after, move_ms=_ms(t0))

    def snap(self, target: str, side: str) -> dict[str, Any]:
        """Snap one window to a work-area half, verified by geometry.

        Left/right use the native Win+Arrow snap (shell-aware); top and
        bottom halves have no hotkey, so they move/resize directly into
        the layout rectangle. Either way the result is polled against
        the expected rectangle, not assumed from the keypress.
        """
        t0 = time.monotonic()
        if side not in ("left", "right", "top", "bottom"):
            return _fail("invalid_side", side=side, snap_ms=_ms(t0))
        work = work_area(self.computer)
        want = layout_rect(side, work)
        focused = self.focus(target)
        if not focused.get("ok"):
            return {**focused, "ok": False,
                    "reason": focused.get("reason", "focus_failed"),
                    "snap_ms": _ms(t0)}
        hwnd = focused["window"].get("hwnd")
        if side in ("left", "right"):
            try:
                if side == "left":
                    self.computer.hotkey("win", "left")
                else:
                    self.computer.hotkey("win", "right")
            except Exception as exc:
                return _fail("snap_failed", target=target,
                             error=str(exc)[:200], snap_ms=_ms(t0))
        else:
            moved = self.move_resize(target, want["x"], want["y"],
                                     want["width"], want["height"])
            if not moved.get("ok"):
                return {**moved, "snap_ms": _ms(t0)}
        after = self._poll_desc(
            hwnd, lambda desc: rects_match(desc["rect"], want))
        if after is not None and rects_match(after["rect"], want):
            return {"ok": True, "window": after, "side": side,
                    "layout": want, "snap_ms": _ms(t0)}
        return _fail("snap_not_reached", target=target, window=after,
                     layout=want, snap_ms=_ms(t0))

    def arrange(self, left_target: str, right_target: str) -> dict[str, Any]:
        """Snap two windows left/right against the work area, verified.

        Native snap is preferred over manual geometry: it respects the
        shell, taskbar and DPI scaling instead of fighting them. Both
        final rectangles (position AND size) are checked.
        """
        t0 = time.monotonic()
        work = work_area(self.computer)
        placed: dict[str, Any] = {}
        for target, side in ((left_target, "left"),
                             (right_target, "right")):
            one = self.snap(target, side)
            if not one.get("ok"):
                return {**one, "ok": False,
                        "reason": one.get("reason", "snap_failed"),
                        "arrange_ms": _ms(t0)}
            placed[target] = one["window"]
        return {"ok": True, "left": placed[left_target],
                "right": placed[right_target],
                "layout": {"left": layout_rect("left", work),
                           "right": layout_rect("right", work)},
                "arrange_ms": _ms(t0)}

    # ------------------------------------------------------------------
    # HEALTH — hung detection + graceful close/restart of one hwnd
    # ------------------------------------------------------------------

    def health(self, target: str) -> dict[str, Any]:
        """Is the window's thread pumping messages?

        Primary probe is ``user32.IsHungAppWindow`` (authoritative
        kernel verdict, no message-result ambiguity). A ``WM_NULL``
        ``SendMessageTimeout`` round-trip is supplementary timing
        evidence — its result word is 0 even when healthy, so only its
        return code is read.
        """
        t0 = time.monotonic()
        resolved = self.resolve(target)
        if not resolved.get("ok") or not resolved.get("found"):
            return {**resolved, "ok": False,
                    "reason": resolved.get("reason", "target not found"),
                    "health_ms": _ms(t0)}
        window = resolved["window"]
        hung = _is_hung(window["hwnd"])
        if hung is None:
            return {"ok": True, "window": window, "responsive": True,
                    "probed": False, "health_ms": _ms(t0)}
        smt_ok: bool | None = None
        w32 = _win32()
        if w32 is not None:
            try:
                import win32con
                ret = w32.SendMessageTimeout(
                    window["hwnd"], win32con.WM_NULL, 0, 0,
                    win32con.SMTO_ABORTIFHUNG, HUNG_PROBE_TIMEOUT_MS)
                smt_ok = bool(ret[0] if isinstance(ret, tuple) else ret)
            except Exception:
                smt_ok = None
        responsive = not hung and smt_ok is not False
        return {"ok": True, "window": window, "responsive": responsive,
                "probed": True, "hung": hung, "smt_ok": smt_ok,
                "health_ms": _ms(t0)}

    def close_window(self, target: str,
                     timeout: float = CLOSE_TIMEOUT_S) -> dict[str, Any]:
        """Graceful WM_CLOSE to one hwnd (never taskkill), verified gone."""
        t0 = time.monotonic()
        resolved = self.resolve(target)
        if not resolved.get("ok") or not resolved.get("found"):
            return {**resolved, "ok": False,
                    "reason": resolved.get("reason", "target not found"),
                    "close_ms": _ms(t0)}
        window = resolved["window"]
        w32 = _win32()
        if w32 is None:
            return _fail("close_unsupported", target=target.strip(),
                         close_ms=_ms(t0))
        try:
            import win32con
            w32.PostMessage(window["hwnd"], win32con.WM_CLOSE, 0, 0)
        except Exception as exc:
            return _fail("close_failed", target=target.strip(),
                         error=str(exc)[:200], close_ms=_ms(t0))
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if not self._hwnd_present(window["hwnd"], window["title"]):
                return {"ok": True, "closed": window["title"],
                        "close_ms": _ms(t0)}
            time.sleep(0.4)
        return _fail("close_not_reached", target=target.strip(),
                     window=window, close_ms=_ms(t0))

    def restart_app(self, target: str, app: str | None = None,
                    timeout: float = 15.0) -> dict[str, Any]:
        """Graceful close + relaunch through the app launcher."""
        t0 = time.monotonic()
        if self.desktop is None:
            return _fail("restart_unsupported",
                         detail="no application launcher wired",
                         restart_ms=_ms(t0))
        closed = self.close_window(target)
        if not closed.get("ok"):
            return {**closed, "restart_ms": _ms(t0)}
        name = (app or self.guess_app(closed.get("closed", "")) or "").strip()
        if not name:
            return _fail("restart_failed",
                         detail="cannot infer application name",
                         restart_ms=_ms(t0))
        try:
            launched = bool(self.desktop.open_app(name))
        except Exception as exc:
            return _fail("restart_failed", app=name,
                         error=str(exc)[:200], restart_ms=_ms(t0))
        if not launched:
            return _fail("restart_failed", app=name,
                         detail="launcher refused", restart_ms=_ms(t0))
        found = self.computer.wait_for_window(name, timeout=timeout)
        return {"ok": bool(found), "app": name,
                "restart_ms": _ms(t0),
                **({} if found else {"reason": "window_not_back"})}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _remember(self, hwnd: int) -> None:
        if hwnd in self._focus_history:
            self._focus_history.remove(hwnd)
        self._focus_history.append(hwnd)
        del self._focus_history[:-20]

    @staticmethod
    def _foreground_hwnd() -> int | None:
        try:
            win = gw.getActiveWindow()
            return getattr(win, "_hWnd", None) if win else None
        except Exception:
            return None

    def _describe_hwnd(self, hwnd: int) -> dict[str, Any] | None:
        try:
            for w in gw.getAllWindows():
                try:
                    if getattr(w, "_hWnd", None) == hwnd and (w.title or ""):
                        return self.describe_window(w)
                except Exception:
                    continue
        except Exception:
            pass
        return None

    @staticmethod
    def _live(hwnd: int):
        """The live automation object for a handle (title-churn safe).

        Re-looking windows up by title breaks the moment an app renames
        itself (Chrome retitles per tab/navigation). Handles are stable,
        so actions pin the hwnd and re-fetch the object per attempt.
        """
        try:
            for w in gw.getAllWindows():
                try:
                    if getattr(w, "_hWnd", None) == hwnd:
                        return w
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _poll_desc(self, hwnd: int, predicate, timeout: float = STATE_TIMEOUT_S):
        """Poll a handle's description until ``predicate`` holds it.

        Replaces single-sample-then-sleep verification: DWM/Chrome
        animations cross the 300ms mark regularly, and one mid-flight
        sample used to report phantom failures. Returns the matching
        description, or the last seen one (None if never observed).
        """
        deadline = time.monotonic() + max(0.2, timeout)
        last = None
        while time.monotonic() < deadline:
            last = self._describe_hwnd(hwnd)
            if last is not None:
                try:
                    if predicate(last):
                        return last
                except Exception:
                    pass
            time.sleep(STATE_POLL_S)
        return last

    def _hwnd_present(self, hwnd: int, title: str) -> bool:
        if not _is_alive(hwnd):
            return False
        try:
            for w in gw.getWindowsWithTitle(title):
                try:
                    if getattr(w, "_hWnd", None) == hwnd:
                        return True
                except Exception:
                    continue
        except Exception:
            return True  # unreadable: do not claim it is gone
        return False
