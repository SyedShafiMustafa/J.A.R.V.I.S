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
        if not self.computer.focus_window(window["title"]):
            return _fail("focus_failed", target=target.strip(),
                         window=window, focus_ms=_ms(t0))
        # Verify by hwnd, not title: titles can repeat, handles cannot.
        confirmed = self._foreground_hwnd() == window["hwnd"]
        if confirmed:
            self._remember(window["hwnd"])
            return {"ok": True, "window": self.active(),
                    "resolved": window, "focus_ms": _ms(t0)}
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
        try:
            matches = gw.getWindowsWithTitle(window["title"])
            live = next((w for w in matches
                         if getattr(w, "_hWnd", None) == window["hwnd"]),
                        None)
            if live is None:
                return _fail("stale_window", target=target.strip(),
                             window=window, state_ms=_ms(t0))
            if state == "minimize":
                live.minimize()
            elif state == "maximize":
                live.maximize()
            else:
                live.restore()
            time.sleep(FOCUS_SETTLE_S)
        except Exception as exc:
            return _fail("state_failed", target=target.strip(),
                         error=str(exc)[:200], state_ms=_ms(t0))
        after = self._describe_hwnd(window["hwnd"])
        good = after is not None and (
            (state == "minimize" and after["minimized"])
            or (state == "maximize" and after["maximized"])
            or (state == "restore"
                and not after["minimized"] and not after["maximized"]))
        if good:
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
        try:
            matches = gw.getWindowsWithTitle(window["title"])
            live = next((w for w in matches
                         if getattr(w, "_hWnd", None) == window["hwnd"]),
                        None)
            if live is None:
                return _fail("stale_window", target=target.strip(),
                             move_ms=_ms(t0))
            try:
                live.restore()  # maximized windows ignore geometry calls
            except Exception:
                pass
            live.moveTo(x, y)
            live.resizeTo(width, height)
            time.sleep(FOCUS_SETTLE_S)
        except Exception as exc:
            return _fail("move_failed", target=target.strip(),
                         error=str(exc)[:200], move_ms=_ms(t0))
        after = self._describe_hwnd(window["hwnd"])
        if after is None:
            return _fail("stale_window", target=target.strip(),
                         move_ms=_ms(t0))
        rect = after["rect"]
        close = (abs(rect["x"] - x) <= RECT_TOLERANCE_PX
                 and abs(rect["y"] - y) <= RECT_TOLERANCE_PX
                 and abs(rect["width"] - width) <= RECT_TOLERANCE_PX
                 and abs(rect["height"] - height) <= RECT_TOLERANCE_PX)
        # DWM clamps/resizes decorated frames: position match is the
        # binding check, size match is best-effort evidence.
        positioned = (abs(rect["x"] - x) <= RECT_TOLERANCE_PX
                      and abs(rect["y"] - y) <= RECT_TOLERANCE_PX)
        if positioned:
            return {"ok": True, "window": after,
                    "requested": {"x": x, "y": y,
                                  "width": width, "height": height},
                    "size_exact": bool(close), "move_ms": _ms(t0)}
        return _fail("move_not_reached", target=target.strip(),
                     window=after, move_ms=_ms(t0))

    def snap(self, target: str, side: str) -> dict[str, Any]:
        """Snap one window to a screen half (Win+Left / Win+Right)."""
        t0 = time.monotonic()
        if side not in ("left", "right"):
            return _fail("invalid_side", side=side, snap_ms=_ms(t0))
        size = self.computer.screen_size()
        if size is None:
            return _fail("no_screen_size", snap_ms=_ms(t0))
        focused = self.focus(target)
        if not focused.get("ok"):
            return {**focused, "ok": False,
                    "reason": focused.get("reason", "focus_failed"),
                    "snap_ms": _ms(t0)}
        try:
            if side == "left":
                self.computer.hotkey("win", "left")
            else:
                self.computer.hotkey("win", "right")
            time.sleep(0.8)
        except Exception as exc:
            return _fail("snap_failed", target=target,
                         error=str(exc)[:200], snap_ms=_ms(t0))
        after = self._describe_hwnd(focused["window"].get("hwnd"))
        if after is None:
            return _fail("stale_window", target=target, snap_ms=_ms(t0))
        want_x = 0 if side == "left" else size["width"] // 2
        if abs(after["rect"].get("x", -999) - want_x) \
                <= RECT_TOLERANCE_PX * 2:
            return {"ok": True, "window": after, "side": side,
                    "snap_ms": _ms(t0)}
        return _fail("snap_not_reached", target=target, window=after,
                     snap_ms=_ms(t0))

    def arrange(self, left_target: str, right_target: str) -> dict[str, Any]:
        """Snap two windows left/right (Win+Left / Win+Right), verified.

        Native snap is preferred over manual geometry: it respects the
        shell, taskbar and DPI scaling instead of fighting them.
        """
        t0 = time.monotonic()
        size = self.computer.screen_size()
        if size is None:
            return _fail("no_screen_size", arrange_ms=_ms(t0))
        half = size["width"] // 2
        placed: dict[str, Any] = {}
        for target, key in ((left_target, "win+left"),
                            (right_target, "win+right")):
            focused = self.focus(target)
            if not focused.get("ok"):
                return {**focused, "ok": False,
                        "reason": focused.get("reason", "focus_failed"),
                        "arrange_ms": _ms(t0)}
            try:
                if key == "win+left":
                    self.computer.hotkey("win", "left")
                else:
                    self.computer.hotkey("win", "right")
                time.sleep(0.8)
            except Exception as exc:
                return _fail("snap_failed", target=target,
                             error=str(exc)[:200], arrange_ms=_ms(t0))
            placed[target] = self._describe_hwnd(
                focused["window"].get("hwnd"))
        left_rect = (placed[left_target] or {}).get("rect", {})
        right_rect = (placed[right_target] or {}).get("rect", {})
        left_ok = abs(left_rect.get("x", -999)) <= RECT_TOLERANCE_PX * 2
        right_ok = (abs(right_rect.get("x", -999) - half)
                    <= RECT_TOLERANCE_PX * 2)
        if left_ok and right_ok:
            return {"ok": True, "left": placed[left_target],
                    "right": placed[right_target],
                    "arrange_ms": _ms(t0)}
        return _fail("arrange_not_reached", left=placed.get(left_target),
                     right=placed.get(right_target),
                     arrange_ms=_ms(t0))

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
