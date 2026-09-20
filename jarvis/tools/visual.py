"""
tools/visual.py
----------------
Milestone 3 — visual computer agent.

Turns JARVIS from a command/tool agent into a visual computer agent that
follows the loop::

    OBSERVE -> UNDERSTAND -> LOCATE -> ACT -> OBSERVE -> VERIFY
                                                 -> RECOVER / RETRY

This module reuses the existing Milestone 2 infrastructure instead of
duplicating it:

- ``ComputerController`` (tools/computer.py) for raw mouse/keyboard/window ops
- ``ScreenVision`` (tools/vision.py) for screenshots + OCR
- ``UIAutomation`` (tools/ui_automation.py) for deterministic UI structure
- ``PermissionEngine`` levels, ``ToolResult`` contracts, experience memory
  (wired in executor / registry / planner, not here)

Design rules honored here:

- Never invent coordinates: every point comes from an observed element.
- Target selection (``locate``) is separated from input execution.
- Never report success merely because an input event fired: ``visual_click``
  and ``visual_type`` re-observe and verify, and return structured evidence.
- When confidence is low the locator refuses instead of blind-clicking.
- Screenshots live in memory by default and are never written to disk by
  this module; typed text is never echoed into logs or messages (only its
  length); OCR text is truncated before it leaves this layer.
- Expensive work is bounded: targeted captures first, OCR only on the
  region being inspected, UIA structure before broad reasoning, bounded
  retries with alternate strategies.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import pygetwindow as gw

from tools.computer import ComputerController
from tools.vision import ScreenVision
from tools.ui_automation import UIAutomation

_log = logging.getLogger("jarvis.visual")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Loop stages, also used as HUD labels (see backend/server.py phases).
STAGES = ("observe", "locate", "act", "verify", "recover")

STAGE_LABELS = {
    "observe": "VISUAL OBSERVE",
    "locate": "LOCATING TARGET",
    "act": "ACTION",
    "verify": "VERIFYING",
    "recover": "RECOVERING",
}

CAPTURE_MODES = ("active", "full", "region")
CLICK_BUTTONS = ("left", "right", "double")
VERIFY_KINDS = ("text_visible", "text_absent", "window_active", "window_closed")

#: Below this confidence the locator refuses instead of clicking.
DEFAULT_MIN_CONFIDENCE = 0.6

#: Initial attempt + this many retries (bounded — never infinite loops).
DEFAULT_MAX_RETRIES = 2

MAX_TEXT_LEN = 2000
MAX_ELEMENTS = 100
OCR_TEXT_LIMIT = 2000
POLL_INTERVAL_S = 0.4

#: Same allow-list as TaskExecutor._validate_step — single chars also allowed.
PRESS_ALLOWLIST = {
    "enter", "escape", "esc", "tab", "space", "backspace", "delete",
    "up", "down", "left", "right", "home", "end", "ctrl", "shift",
    "alt", "win",
}

#: Natural-language hints -> structured element types.
_TYPE_HINTS = {
    "button": "button", "btn": "button",
    "menu": "menu",
    "field": "text_field", "box": "text_field", "input": "text_field",
    "search": "text_field",
    "tab": "tab",
    "link": "link",
    "dialog": "dialog", "window": "window", "popup": "dialog",
    "checkbox": "checkbox", "check": "checkbox",
}

#: UIA control types -> structured element types.
_UIA_TYPES = {
    "Button": "button",
    "Edit": "text_field",
    "Document": "document",
    "Text": "text",
    "ListItem": "list_item",
    "MenuItem": "menu",
    "Menu": "menu",
    "CheckBox": "checkbox",
    "ComboBox": "combobox",
    "TabItem": "tab",
    "Hyperlink": "link",
}

_STOPWORDS = {
    "the", "a", "an", "this", "that", "please", "click", "press",
    "open", "close", "on", "in", "at", "to",
}


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _words_near(hay: str, needle: str, slack: int = 4) -> tuple[bool, int]:
    """Are all ``needle`` words visible close together in ``hay``?

    Tesseract sometimes scrambles reading order on small windows
    ("visual JARVIS test|" for "JARVIS visual test") and glues the text
    cursor onto words. Exact and alignment matching then fail even
    though every word is on screen. This accepts the text only when all
    needle words occur inside one tight span of hay words, so words
    scattered across unrelated menus cannot fake a match.
    """
    hay_words = hay.lower().split()
    needle_words = [w for w in needle.lower().split() if w]
    if not needle_words or len(needle_words) > len(hay_words):
        return False, -1

    def _word_hit(nw: str, hw: str) -> bool:
        if nw == hw:
            return True
        if len(nw) >= 4 and (nw in hw or hw in nw):
            return True
        return False

    span = len(needle_words) + max(0, slack)
    for i in range(len(hay_words) - len(needle_words) + 1):
        window = hay_words[i:i + span]
        if all(any(_word_hit(nw, hw) for hw in window)
               for nw in needle_words):
            return True, span
    return False, -1


def _fuzzy_contains(hay: str, needle: str, threshold: float = 0.8) -> tuple[bool, float]:
    """Best-alignment similarity of ``needle`` inside ``hay``.

    OCR mangles single characters ("JARV1S") while keeping the shape of
    the text. Exact match is tried first; otherwise the needle slides
    over the haystack and the best SequenceMatcher ratio wins.
    """
    import difflib
    if not needle or len(needle) > len(hay):
        return False, 0.0
    best = 0.0
    width = len(needle)
    for i in range(len(hay) - width + 1):
        ratio = difflib.SequenceMatcher(
            None, needle, hay[i:i + width]).ratio()
        if ratio > best:
            best = ratio
            if best >= 1.0:
                break
    return best >= threshold, round(best, 3)


def _pixels(image: Any) -> list:
    """Grayscale pixel values, Pillow-14 safe (getdata is deprecated)."""
    fn = getattr(image, "get_flattened_data", None)
    if callable(fn):
        return list(fn())
    return list(image.getdata())


def _fail(reason: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "reason": reason}
    out.update(extra)
    return out


class VisualAgent:
    """Observe/understand/locate/act/verify/recover over the live screen."""

    def __init__(
        self,
        computer: ComputerController | None = None,
        vision: ScreenVision | None = None,
        uia: UIAutomation | None = None,
    ) -> None:
        self.computer = computer or ComputerController()
        self.vision = vision or ScreenVision()
        self.uia = uia or UIAutomation()
        self.last_error: str | None = None

    # ------------------------------------------------------------------
    # OBSERVE — screen capture + active-window awareness
    # ------------------------------------------------------------------

    def capture(
        self,
        mode: str = "active",
        region: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Capture the screen in a structured, testable envelope.

        ``mode`` is ``active`` (focused window), ``full`` (primary screen)
        or ``region`` (explicit ``{"x","y","width","height"}`` clamped to
        the screen). The PIL image is returned in memory under ``"image"``
        and must never cross a process/WS boundary — callers strip it.
        """
        t0 = time.monotonic()
        if mode not in CAPTURE_MODES:
            return _fail("invalid_mode", mode=mode, screenshot_ms=_ms(t0))

        clamped = None
        if mode == "region":
            clamped = self._clamp_region(region)
            if clamped is None:
                return _fail(
                    "invalid_region", region=region, screenshot_ms=_ms(t0)
                )
            shot = self.vision.capture_shot("region", clamped)
        else:
            shot = self.vision.capture_shot(mode)

        if not shot.get("ok"):
            self.last_error = shot.get("error") or "capture unavailable"
            return _fail(
                "capture_unavailable",
                mode=mode,
                error=self.last_error,
                screenshot_ms=_ms(t0),
            )

        self.last_error = None
        return {
            "ok": True,
            "mode": mode,
            "width": shot["width"],
            "height": shot["height"],
            "origin": {"x": shot["origin"][0], "y": shot["origin"][1]},
            "region": clamped,
            "image": shot["image"],
            "screenshot_ms": _ms(t0),
        }

    def _clamp_region(self, region: Any) -> dict[str, int] | None:
        if not isinstance(region, dict):
            return None
        try:
            x, y = int(region["x"]), int(region["y"])
            w, h = int(region["width"]), int(region["height"])
        except (KeyError, TypeError, ValueError):
            return None
        if w < 1 or h < 1 or x < 0 or y < 0:
            return None
        size = self.computer.screen_size()
        if size is not None:
            # Origin completely off-screen: nothing to capture.
            if x >= size["width"] or y >= size["height"]:
                return None
            w = min(w, max(1, size["width"] - x))
            h = min(h, max(1, size["height"] - y))
            if w < 1 or h < 1:
                return None
        return {"x": x, "y": y, "width": w, "height": h}

    def active_window(self) -> dict[str, Any]:
        """Describe the focused window: title, app hint and bounds."""
        title = self.computer.get_active_window()
        if not title:
            return {"available": False, "title": None, "app": None,
                    "bounds": None}
        bounds = self.vision.window_bounds()
        app = title.rsplit(" - ", 1)[-1].strip() or title
        return {
            "available": True,
            "title": title,
            "app": app,
            "bounds": (
                {"x": bounds[0], "y": bounds[1],
                 "width": bounds[2], "height": bounds[3]}
                if bounds else None
            ),
        }

    # ------------------------------------------------------------------
    # UNDERSTAND — structured screen model (UIA structure + OCR text)
    # ------------------------------------------------------------------

    def inspect(
        self,
        mode: str = "active",
        region: dict[str, Any] | None = None,
        max_elements: int = MAX_ELEMENTS,
        include_uia: bool = True,
    ) -> dict[str, Any]:
        """Fuse deterministic UIA structure with OCR text into elements::

            {"type","label","region","point","confidence","source"}

        Never hallucinates: only observed controls/words are returned, and
        ``uncertain`` is True when nothing usable was seen.
        """
        t0 = time.monotonic()
        shot = self.capture(mode=mode, region=region)
        if not shot.get("ok"):
            return {**shot, "elements": [], "text": "",
                    "uncertain": True, "inspect_ms": _ms(t0)}

        win = self.active_window()
        elements: list[dict[str, Any]] = []

        # 1. Deterministic UI structure first (no OCR cost, real control
        #    types and bounds straight from the OS accessibility tree).
        #    Skipped for tight verify polls: some dialogs (file pickers,
        #    shell views) make tree walks cost seconds, while a text
        #    check only needs OCR.
        uia_ms = 0
        if include_uia and mode in ("active", "full") and region is None:
            u0 = time.monotonic()
            for item in self.uia.safe_inspect():
                el = self._uia_element(item)
                if el is not None:
                    elements.append(el)
            uia_ms = _ms(u0)

        # 2. OCR text with coordinates on the captured region.
        o0 = time.monotonic()
        ox, oy = shot["origin"]["x"], shot["origin"]["y"]
        for item in self.vision.read_shot_elements(
            shot["image"], origin=(ox, oy)
        ):
            elements.append({
                "type": "text",
                "label": item["text"],
                "region": {"x": item["x"], "y": item["y"],
                           "width": item["w"], "height": item["h"]},
                "point": {"x": item["cx"], "y": item["cy"]},
                "confidence": round(item["confidence"] / 100.0, 3),
                "source": "ocr",
            })
        ocr_ms = _ms(o0)

        elements = self._dedupe(elements)[:max(1, max_elements)]
        text = " ".join(
            e["label"] for e in elements if e["source"] == "ocr"
        )[:OCR_TEXT_LIMIT]

        return {
            "ok": True,
            "mode": mode,
            "window": win.get("title"),
            "app": win.get("app"),
            "bounds": win.get("bounds"),
            "elements": elements,
            "element_count": len(elements),
            "text": text,
            "uncertain": not elements,
            "timings": {
                "screenshot_ms": shot["screenshot_ms"],
                "uia_ms": uia_ms,
                "ocr_ms": ocr_ms,
                "inspect_ms": _ms(t0),
            },
        }

    @staticmethod
    def _uia_element(item: dict[str, Any]) -> dict[str, Any] | None:
        try:
            name = str(item.get("name") or "").strip()
            left, top = int(item["left"]), int(item["top"])
            right, bottom = int(item["right"]), int(item["bottom"])
        except (KeyError, TypeError, ValueError):
            return None
        if not name or right <= left or bottom <= top:
            return None
        etype = _UIA_TYPES.get(str(item.get("type") or ""),
                               str(item.get("type") or "control").lower())
        return {
            "type": etype,
            "label": name,
            "region": {"x": left, "y": top,
                       "width": right - left, "height": bottom - top},
            "point": {"x": (left + right) // 2, "y": (top + bottom) // 2},
            "confidence": 0.9,
            "source": "uia",
        }

    @staticmethod
    def _dedupe(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Same label at (nearly) the same point is one target even when
        # seen twice (e.g. a UIA Button "Save" plus the OCR word "Save"
        # painted on it). Merge those, keeping the deterministic UIA
        # record and the best confidence of either sighting.
        seen: list[dict[str, Any]] = []
        for el in elements:
            dup = False
            for kept in seen:
                if (kept["label"].lower() == el["label"].lower()
                        and abs(kept["point"]["x"] - el["point"]["x"]) <= 8
                        and abs(kept["point"]["y"] - el["point"]["y"]) <= 8):
                    best = max(kept["confidence"], el["confidence"])
                    if el["source"] == "uia":
                        kept.update(el)
                    kept["confidence"] = best
                    dup = True
                    break
            if not dup:
                seen.append(el)
        # Deterministic structure first, then OCR text.
        seen.sort(key=lambda e: (0 if e["source"] == "uia" else 1,
                                 e["point"]["y"], e["point"]["x"]))
        return seen

    # ------------------------------------------------------------------
    # LOCATE — natural-language target -> ranked region with confidence
    # ------------------------------------------------------------------

    def locate(
        self,
        target: str,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        mode: str = "active",
    ) -> dict[str, Any]:
        """Rank observed elements against ``target`` ("the Save button").

        Returns the best region/point with confidence, or refuses with
        ``found: False`` when nothing meets ``min_confidence``. Pure
        observation — performs no input.
        """
        t0 = time.monotonic()
        if not isinstance(target, str) or not target.strip():
            return _fail("invalid_target", target=target,
                         locate_ms=_ms(t0))
        try:
            min_conf = float(min_confidence)
        except (TypeError, ValueError):
            return _fail("invalid_confidence", min_confidence=min_confidence,
                         locate_ms=_ms(t0))
        min_conf = min(1.0, max(0.0, min_conf))

        model = self.inspect(mode=mode)
        if not model.get("ok"):
            return _fail("observe_failed", target=target.strip(),
                         error=model.get("reason"),
                         locate_ms=_ms(t0))

        words = [w for w in target.lower().split()
                 if w not in _STOPWORDS]
        keywords = words or target.lower().split()
        desired = {_TYPE_HINTS[w] for w in keywords if w in _TYPE_HINTS}

        ranked: list[dict[str, Any]] = []
        for el in model["elements"]:
            conf = self._score(el, keywords, desired)
            if conf <= 0:
                continue
            ranked.append({
                "label": el["label"],
                "type": el["type"],
                "point": el["point"],
                "region": el["region"],
                "confidence": conf,
                "source": el["source"],
            })
        ranked.sort(key=lambda c: c["confidence"], reverse=True)

        if not ranked or ranked[0]["confidence"] < min_conf:
            self.last_error = "target not found"
            return {
                "ok": True,
                "found": False,
                "target": target.strip(),
                "reason": "low_confidence",
                "best": ranked[0] if ranked else None,
                "candidates": ranked[:5],
                "uncertain": True,
                "min_confidence": min_conf,
                "locate_ms": _ms(t0),
            }

        self.last_error = None
        best = ranked[0]
        return {
            "ok": True,
            "found": True,
            "target": target.strip(),
            "located": best,
            "candidates": ranked[:5],
            "uncertain": False,
            "min_confidence": min_conf,
            "locate_ms": _ms(t0),
        }

    @staticmethod
    def _score(el: dict[str, Any], keywords: list[str],
               desired: set[str]) -> float:
        label = el["label"].lower()
        label_words = label.split()
        if not keywords:
            return 0.0
        if label == " ".join(keywords):
            base = 1.0
        elif all(k in label_words for k in keywords):
            base = 0.9
        elif any(k == w for k in keywords for w in label_words):
            hits = sum(1 for k in keywords if k in label_words)
            base = 0.4 + 0.4 * (hits / len(keywords))
        elif any(k in label or label in k for k in keywords):
            base = 0.55
        else:
            return 0.0
        bonus = 0.0
        if desired and el["type"] in desired:
            bonus += 0.05
        if el["source"] == "uia":
            bonus += 0.04
        return round(min(0.99, base + bonus), 3)

    # ------------------------------------------------------------------
    # ACT — validated, timed, structured mouse / keyboard actions
    # ------------------------------------------------------------------

    def _check_point(self, point: Any) -> tuple[int, int] | None:
        try:
            x, y = int(point["x"]), int(point["y"])
        except (KeyError, TypeError, ValueError):
            return None
        if x < 0 or y < 0:
            return None
        size = self.computer.screen_size()
        if size is not None and (
            x >= size["width"] or y >= size["height"]
        ):
            return None
        return (x, y)

    def _timed(self, action: str, fn: Callable[[], None],
               timeout: float) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            fn()
        except Exception as exc:
            _log.warning("visual %s failed: %s", action, exc)
            return _fail("action_failed", action=action, error=str(exc)[:200],
                         action_ms=_ms(t0))
        elapsed_ms = _ms(t0)
        if elapsed_ms > timeout * 1000:
            return _fail("timeout", action=action,
                         action_ms=elapsed_ms)
        return {"ok": True, "action": action, "action_ms": elapsed_ms}

    def move_to(self, point: dict[str, int],
                timeout: float = 5.0) -> dict[str, Any]:
        xy = self._check_point(point)
        if xy is None:
            return _fail("invalid_point", action="move", point=point)
        res = self._timed("move", lambda: self.computer.move(*xy),
                          timeout)
        return {**res, "point": {"x": xy[0], "y": xy[1]}}

    def click_point(self, point: dict[str, int], button: str = "left",
                    timeout: float = 5.0) -> dict[str, Any]:
        if button not in CLICK_BUTTONS:
            return _fail("invalid_button", action="click",
                         button=button)
        xy = self._check_point(point)
        if xy is None:
            return _fail("invalid_point", action="click", point=point)
        if button == "left":
            fn = lambda: self.computer.click(*xy)
        elif button == "right":
            fn = lambda: self.computer.right_click(*xy)
        else:
            fn = lambda: self.computer.double_click(*xy)
        res = self._timed("click", fn, timeout)
        return {**res, "button": button,
                "point": {"x": xy[0], "y": xy[1]}}

    def drag_points(self, from_point: dict[str, int],
                    to_point: dict[str, int], duration: float = 0.4,
                    timeout: float = 10.0) -> dict[str, Any]:
        src = self._check_point(from_point)
        dst = self._check_point(to_point)
        if src is None or dst is None:
            return _fail("invalid_point", action="drag",
                         from_point=from_point, to_point=to_point)

        def _do() -> None:
            self.computer.move(*src)
            self.computer.drag(*dst, duration=duration)

        res = self._timed("drag", _do, timeout)
        return {**res, "from": {"x": src[0], "y": src[1]},
                "to": {"x": dst[0], "y": dst[1]}}

    def scroll_at(self, amount: int, point: dict[str, int] | None = None,
                  timeout: float = 5.0) -> dict[str, Any]:
        if isinstance(amount, bool) or not isinstance(amount, int):
            return _fail("invalid_amount", action="scroll",
                         amount=amount)
        if point is not None:
            moved = self.move_to(point, timeout=timeout)
            if not moved.get("ok"):
                return {**moved, "action": "scroll"}
        res = self._timed("scroll",
                          lambda: self.computer.scroll(amount), timeout)
        return {**res, "amount": amount}

    def type_text(self, text: str,
                  click_point: dict[str, int] | None = None,
                  expected_window: str | None = None,
                  timeout: float = 10.0) -> dict[str, Any]:
        """Type into the focused (or clicked) field with focus safety.

        The text itself is never echoed back — only its length.
        """
        if not isinstance(text, str) or not text:
            return _fail("invalid_text", action="type")
        if len(text) > MAX_TEXT_LEN:
            return _fail("text_too_long", action="type",
                         max_chars=MAX_TEXT_LEN)
        if expected_window:
            active = self.computer.get_active_window() or ""
            if expected_window.lower() not in active.lower():
                return _fail("wrong_window", action="type",
                             expected_window=expected_window,
                             active_window=active or None)
        if click_point is not None:
            clicked = self.click_point(click_point, timeout=timeout)
            if not clicked.get("ok"):
                return {**clicked, "action": "type"}
        # Clipboard paste is fast and unicode-safe; fall back to keystroke
        # injection if the clipboard path is unavailable.
        try:
            self.computer.set_clipboard(text)
            res = self._timed("type", self.computer.paste, timeout)
        except Exception:
            res = self._timed(
                "type", lambda: self.computer.type_text(text), timeout)
        # Never log or return the typed content itself.
        _log.info("visual type: %d chars typed ok=%s", len(text),
                  res.get("ok"))
        return {**res, "typed_chars": len(text)}

    def press_key(self, key: str,
                  timeout: float = 5.0) -> dict[str, Any]:
        if not isinstance(key, str) or not key.strip():
            return _fail("invalid_key", action="press", key=key)
        k = key.strip()
        if k.lower() not in PRESS_ALLOWLIST and len(k) != 1:
            return _fail("invalid_key", action="press", key=key)
        return self._timed("press", lambda: self.computer.press(k),
                           timeout)

    def hotkeys(self, keys: list[str],
                timeout: float = 5.0) -> dict[str, Any]:
        if (not isinstance(keys, list) or not keys
                or not all(isinstance(k, str) and k.strip()
                           for k in keys)):
            return _fail("invalid_hotkey", action="hotkey", keys=keys)
        return self._timed(
            "hotkey", lambda: self.computer.hotkey(*keys), timeout)

    # ------------------------------------------------------------------
    # VERIFY — re-observe and check the expected visual state
    # ------------------------------------------------------------------

    def verify(self, expectation: dict[str, Any],
               timeout: float = 10.0) -> dict[str, Any]:
        """Check ``expectation`` against fresh observations.

        Kinds: ``text_visible`` / ``text_absent`` (``text``), or
        ``window_active`` / ``window_closed`` (``title``). Returns
        ``{verified, evidence}`` — never a bare claim.
        """
        t0 = time.monotonic()
        if not isinstance(expectation, dict):
            return _fail("invalid_expectation", expectation=expectation,
                         verify_ms=_ms(t0))
        kind = expectation.get("kind")
        if kind not in VERIFY_KINDS:
            return _fail("invalid_expectation", expectation=expectation,
                         verify_ms=_ms(t0))

        deadline = time.monotonic() + max(0.1, timeout)
        evidence: dict[str, Any] = {}
        while True:
            if kind in ("text_visible", "text_absent"):
                needle = str(expectation.get("text") or "")
                if not needle.strip():
                    return _fail("invalid_expectation",
                                 expectation=expectation,
                                 verify_ms=_ms(t0))
                # OCR-only: text checks never pay the UIA tree-walk cost.
                model = self.inspect(include_uia=False)
                hay = model.get("text", "") if model.get("ok") else ""
                low_hay = " ".join(hay.lower().split())
                low_needle = " ".join(needle.lower().split())
                found = low_needle in low_hay
                match = "exact" if found else "none"
                score = 1.0 if found else 0.0
                if not found and kind == "text_visible":
                    # OCR mangles single characters ("JARV1S"): fall back
                    # to alignment similarity instead of failing.
                    found, score = _fuzzy_contains(low_hay, low_needle)
                    if found:
                        match = f"fuzzy:{score}"
                if not found and kind == "text_visible":
                    # OCR scrambles reading order on small windows
                    # ("visual JARVIS test|"): accept all words in one
                    # tight span as evidence, nothing looser.
                    near, span = _words_near(low_hay, low_needle)
                    if near:
                        found = True
                        match = f"words:span{span}"
                evidence = {"window": model.get("window"),
                            "text_found": found,
                            "match": match,
                            "chars_observed": len(hay)}
                good = found if kind == "text_visible" else not found
            elif kind == "window_active":
                title = str(expectation.get("title") or "")
                active = self.computer.get_active_window() or ""
                evidence = {"active_window": active or None}
                good = bool(title.strip()) and title.lower() in active.lower()
            else:  # window_closed
                title = str(expectation.get("title") or "")
                if not title.strip():
                    return _fail("invalid_expectation",
                                 expectation=expectation,
                                 verify_ms=_ms(t0))
                try:
                    # Read-only check: focus_window would steal focus.
                    still = bool(gw.getWindowsWithTitle(title))
                except Exception:
                    still = False
                evidence = {"window_present": still}
                good = not still

            if good or time.monotonic() >= deadline:
                return {"ok": True, "verified": bool(good), "kind": kind,
                        "evidence": evidence, "verify_ms": _ms(t0)}
            time.sleep(POLL_INTERVAL_S)

    # ------------------------------------------------------------------
    # Guarded high-level ops: locate -> act -> observe -> verify -> recover
    # ------------------------------------------------------------------

    @staticmethod
    def shots_differ(before: Any, after: Any,
                     threshold: float = 2.0) -> bool:
        """Did the screen visibly change between two captures?"""
        try:
            a = before.convert("L").resize((64, 64))
            b = after.convert("L").resize((64, 64))
            pa, pb = _pixels(a), _pixels(b)
            rms = (sum((x - y) ** 2 for x, y in zip(pa, pb))
                   / len(pa)) ** 0.5
            return rms > threshold
        except Exception:
            return False

    def run_guarded(
        self,
        action_fn: Callable[[], dict[str, Any]],
        verify_fn: Callable[[], dict[str, Any]],
        max_retries: int = DEFAULT_MAX_RETRIES,
        recover_fn: Callable[[int, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run ``action_fn`` then ``verify_fn`` with bounded recovery.

        On verification failure a fresh observation is taken, an alternate
        strategy runs (``recover_fn`` or a re-observe note), and the action
        retries — at most ``max_retries`` times. Never loops forever.
        """
        t0 = time.monotonic()
        try:
            retries = max(0, int(max_retries))
        except (TypeError, ValueError):
            retries = DEFAULT_MAX_RETRIES
        trail: list[dict[str, Any]] = []
        recovery: dict[str, Any] = {"ok": True, "strategy": "none"}

        for attempt in range(retries + 1):
            if attempt > 0:
                # RECOVER: fresh observation, then an alternate strategy.
                self.capture()
                if recover_fn is not None:
                    try:
                        recovery = recover_fn(attempt, trail[-1])
                    except Exception as exc:
                        recovery = {"ok": False,
                                    "error": str(exc)[:200]}
                else:
                    recovery = {"ok": True, "strategy": "re_observe"}
            action = action_fn()
            verification = verify_fn()
            trail.append({"attempt": attempt + 1, "action": action,
                          "verification": verification,
                          "recovery": dict(recovery)})
            if action.get("ok") and verification.get("verified"):
                return {"ok": True, "attempts": attempt + 1,
                        "verified": True, "trail": trail,
                        "retries": attempt,
                        "total_ms": _ms(t0)}
        return {"ok": False, "attempts": retries + 1, "verified": False,
                "trail": trail, "retries": retries,
                "total_ms": _ms(t0),
                "reason": "verify_failed_after_retries"}

    def visual_click(
        self,
        target: str,
        button: str = "left",
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        expected_window: str | None = None,
        verify_text: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> dict[str, Any]:
        """Locate ``target``, click it, re-observe and verify."""
        t0 = time.monotonic()
        if button not in CLICK_BUTTONS:
            return _fail("invalid_button", target=target, button=button)
        located = self.locate(target, min_confidence=min_confidence)
        if not located.get("ok") or not located.get("found"):
            return {**located, "ok": False,
                    "reason": located.get("reason", "target not found"),
                    "total_ms": _ms(t0)}
        point = located["located"]["point"]
        before = self.capture().get("image")

        if expected_window:
            active = self.computer.get_active_window() or ""
            if expected_window.lower() not in active.lower():
                return _fail("wrong_window", target=target,
                             expected_window=expected_window,
                             active_window=active or None,
                             total_ms=_ms(t0))

        def _act() -> dict[str, Any]:
            return self.click_point(point, button=button)

        if verify_text:
            def _verify() -> dict[str, Any]:
                return self.verify({"kind": "text_visible",
                                    "text": verify_text})
        else:
            def _verify() -> dict[str, Any]:
                after = self.capture().get("image")
                changed = (before is not None and after is not None
                           and self.shots_differ(before, after))
                return {"ok": True, "verified": bool(changed),
                        "kind": "screen_changed",
                        "evidence": {"screen_changed": bool(changed)}}

        result = self.run_guarded(_act, _verify, max_retries=max_retries)
        return {"ok": result["ok"], "target": target,
                "located": located["located"], "button": button,
                "verified": result["verified"],
                "evidence": {"attempts": result["attempts"],
                             "trail": result["trail"]},
                "retries": result["retries"],
                "total_ms": _ms(t0),
                **({} if result["ok"]
                   else {"reason": result.get("reason")})}

    def visual_type(
        self,
        text: str,
        target: str | None = None,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        expected_window: str | None = None,
        verify_text: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> dict[str, Any]:
        """Focus ``target`` (when given), type, re-observe and verify."""
        t0 = time.monotonic()
        if not isinstance(text, str) or not text:
            return _fail("invalid_text", target=target)
        point: dict[str, int] | None = None
        located: dict[str, Any] | None = None
        if target:
            located = self.locate(target, min_confidence=min_confidence)
            if not located.get("ok") or not located.get("found"):
                return {**located, "ok": False,
                        "reason": located.get("reason",
                                              "target not found"),
                        "total_ms": _ms(t0)}
            point = located["located"]["point"]
        before = self.capture().get("image")
        needle = verify_text or text[:24]

        def _act() -> dict[str, Any]:
            return self.type_text(text, click_point=point,
                                  expected_window=expected_window)

        def _verify() -> dict[str, Any]:
            seen = self.verify({"kind": "text_visible", "text": needle})
            if seen.get("verified"):
                return seen
            after = self.capture().get("image")
            changed = (before is not None and after is not None
                       and self.shots_differ(before, after))
            return {"ok": True, "verified": bool(changed),
                    "kind": "screen_changed",
                    "evidence": {"screen_changed": bool(changed),
                                 "text_check": seen}}

        result = self.run_guarded(_act, _verify, max_retries=max_retries)
        out: dict[str, Any] = {
            "ok": result["ok"], "typed_chars": len(text),
            "verified": result["verified"],
            "evidence": {"attempts": result["attempts"],
                         "trail": result["trail"]},
            "retries": result["retries"], "total_ms": _ms(t0),
        }
        if located is not None:
            out["located"] = located["located"]
        if not result["ok"]:
            out["reason"] = result.get("reason")
        return out

    def visual_drag(
        self,
        target: str,
        to_target: str,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> dict[str, Any]:
        """Locate two endpoints ("this file" -> "that folder") and drag."""
        t0 = time.monotonic()
        src = self.locate(target, min_confidence=min_confidence)
        if not src.get("ok") or not src.get("found"):
            return {**src, "ok": False,
                    "reason": src.get("reason", "target not found"),
                    "total_ms": _ms(t0)}
        dst = self.locate(to_target, min_confidence=min_confidence)
        if not dst.get("ok") or not dst.get("found"):
            return {**dst, "ok": False,
                    "reason": dst.get("reason", "target not found"),
                    "total_ms": _ms(t0)}
        before = self.capture().get("image")

        def _act() -> dict[str, Any]:
            return self.drag_points(src["located"]["point"],
                                    dst["located"]["point"])

        def _verify() -> dict[str, Any]:
            after = self.capture().get("image")
            changed = (before is not None and after is not None
                       and self.shots_differ(before, after))
            return {"ok": True, "verified": bool(changed),
                    "kind": "screen_changed",
                    "evidence": {"screen_changed": bool(changed)}}

        result = self.run_guarded(_act, _verify, max_retries=max_retries)
        return {"ok": result["ok"], "from": src["located"],
                "to": dst["located"], "verified": result["verified"],
                "evidence": {"attempts": result["attempts"],
                             "trail": result["trail"]},
                "retries": result["retries"], "total_ms": _ms(t0),
                **({} if result["ok"]
                   else {"reason": result.get("reason")})}

    # ------------------------------------------------------------------
    # Experience memory — structured lessons, never raw screenshots
    # ------------------------------------------------------------------

    @staticmethod
    def build_lesson(action_desc: str,
                     result: dict[str, Any]) -> dict[str, str]:
        ok = bool(result.get("ok") and result.get("verified", True))
        if ok:
            lesson = (f"{action_desc} worked; "
                      f"verified in {result.get('retries', 0)} retries")
        else:
            lesson = (f"{action_desc} failed: "
                      f"{result.get('reason', 'unverified')}; "
                      f"try a shortcut or re-locate before retrying")
        return {
            "scenario": f"visual:{action_desc[:120]}",
            "strategy": str(result.get("evidence", ""))[:300],
            "outcome": "success" if ok else "failure",
            "lesson": lesson[:300],
        }

    def record_lesson(self, memory: Any, action_desc: str,
                      result: dict[str, Any]) -> None:
        save = getattr(memory, "save_experience", None)
        if not callable(save):
            return
        try:
            save(**self.build_lesson(action_desc, result))
        except Exception:
            _log.debug("visual lesson save failed", exc_info=True)
