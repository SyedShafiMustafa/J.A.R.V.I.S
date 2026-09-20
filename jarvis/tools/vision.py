import os
import re
import tempfile

import pyautogui
import pygetwindow as gw
import pytesseract
from PIL import Image


# Sidebar section headers and non-name lines that OCR picks up in the chat
# list / search results pane.
_SECTION_LABELS = {
    "chats", "contacts", "groups in common", "messages", "media", "links",
    "documents", "archived", "archived chats", "unread", "favourites",
    "favorites", "all", "pinned",
}

_CLOCK_RE = re.compile(r"^\d{1,2}[:.]\d{2}([:.]\d{2})?$")
_DATE_RE = re.compile(r"^\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}$")

# Phrases that mark a line as a preview/snippet rather than a chat name.
_PREVIEW_HINTS = (
    "is also in this group", "you:", "click here", "typing", "waiting for",
    "added you", "missed call", "photo", "video", "sticker", "voice message",
    "no chats", "no results", "contacts found",
)


def clean_sidebar_name(text: str) -> str:
    """Best-effort contact/group name from one OCR'd sidebar line.

    Chat rows OCR as ``Name 1:51 pm`` with icon glyphs attached, and the row
    below is a message preview. This returns "" for anything that is clearly
    not a name so callers only ever compare real candidates.
    """
    toks = [t for t in (text or "").split() if t]

    # Trailing clock/date ("Mumma 1:51 pm").
    while toks and (
        _CLOCK_RE.match(toks[-1])
        or _DATE_RE.match(toks[-1])
        or toks[-1].lower() in {"am", "pm"}
    ):
        toks.pop()

    cleaned = []
    for tok in toks:
        tok = tok.strip(".,;:!?*|<>~—–-·@#\"'`^+")
        if tok and any(ch.isalnum() for ch in tok):
            cleaned.append(tok)

    if not cleaned or len(cleaned) > 6:
        return ""

    name = " ".join(cleaned)
    low = name.lower()
    if low in _SECTION_LABELS:
        return ""
    if any(hint in low for hint in _PREVIEW_HINTS):
        return ""
    # "All Unread 2 Favourites" and similar tab strips.
    if "unread" in low or "favourites" in low or "favorites" in low:
        return ""
    return name


class ScreenVision:

    def __init__(self):
        self.last_error = None

    # ---------------------------------------
    # Capture only the active window
    # ---------------------------------------

    def capture(self):

        win = gw.getActiveWindow()

        if win is None:
            return pyautogui.screenshot(), (0, 0)

        left = max(0, win.left)
        top = max(0, win.top)
        width = max(1, win.width)
        height = max(1, win.height)

        img = pyautogui.screenshot(region=(left, top, width, height))

        return img, (left, top)

    # ---------------------------------------
    # Structured capture (Milestone 3): full / active / region
    # ---------------------------------------

    def capture_shot(self, mode="active", region=None):
        """Capture the screen into a structured envelope.

        ``mode`` is ``"active"`` (focused window), ``"full"`` (primary
        screen) or ``"region"`` (explicit ``{"x","y","width","height"}``
        dict, clamped to non-negative sizes). Never raises: failures come
        back as ``{"ok": False, "error": ...}`` so visual loops can
        recover instead of crashing.
        """
        try:
            if mode == "full":
                image = pyautogui.screenshot()
                origin = (0, 0)
            elif mode == "region":
                x = max(0, int(region["x"]))
                y = max(0, int(region["y"]))
                w = max(1, int(region["width"]))
                h = max(1, int(region["height"]))
                image = pyautogui.screenshot(region=(x, y, w, h))
                origin = (x, y)
            else:
                image, origin = self.capture()

            width, height = image.size

            return {
                "ok": True,
                "image": image,
                "origin": origin,
                "width": width,
                "height": height,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    def read_shot_elements(self, image, origin=(0, 0), min_conf=50):
        """OCR ``image`` (PIL, in memory — no temp files) with coordinates.

        Returns ``[{text, x, y, w, h, cx, cy, confidence}]`` where
        ``(x, y)`` is the absolute screen origin of the word box and
        ``(cx, cy)`` its center. Same filtering contract as
        ``read_elements`` so both paths rank identically.
        """
        try:
            data = pytesseract.image_to_data(
                image,
                output_type=pytesseract.Output.DICT
            )
        except Exception:
            return []

        ox, oy = origin
        elements = []

        for i in range(len(data["text"])):

            text = (data["text"][i] or "").strip()

            if not text:
                continue

            try:
                conf = int(float(data["conf"][i]))
            except (TypeError, ValueError):
                conf = 0

            if conf < min_conf:
                continue

            x = data["left"][i]
            y = data["top"][i]
            w = data["width"][i]
            h = data["height"][i]

            elements.append({
                "text": text,
                "x": ox + x,
                "y": oy + y,
                "w": w,
                "h": h,
                "cx": ox + x + w // 2,
                "cy": oy + y + h // 2,
                "confidence": conf
            })

        return elements

    def shot_text(self, image):
        """Whitespace-normalized OCR string for one in-memory image."""
        try:
            text = pytesseract.image_to_string(image)
        except Exception:
            return ""

        return " ".join(text.split())

    # ---------------------------------------
    # OCR with coordinates
    # ---------------------------------------

    def read_elements(self):

        image, offset = self.capture()

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name

        image.save(path)

        data = pytesseract.image_to_data(
            Image.open(path),
            output_type=pytesseract.Output.DICT
        )

        os.remove(path)

        ox, oy = offset
        elements = []

        for i in range(len(data["text"])):

            text = data["text"][i].strip()

            if not text:
                continue

            try:
                conf = int(float(data["conf"][i]))
            except (TypeError, ValueError):
                conf = 0

            if conf < 50:
                continue

            x = data["left"][i]
            y = data["top"][i]
            w = data["width"][i]
            h = data["height"][i]

            elements.append({
                "text": text,
                "x": ox + x + w // 2,
                "y": oy + y + h // 2,
                "confidence": conf
            })

        return elements

    # ---------------------------------------
    # Find text (supports multi-word phrases)
    # ---------------------------------------

    def find_text(self, phrase):

        words = phrase.lower().split()
        elements = self.read_elements()

        for i in range(len(elements)):

            match = True

            for j in range(len(words)):

                if i + j >= len(elements):
                    match = False
                    break

                if words[j] not in elements[i + j]["text"].lower():
                    match = False
                    break

            if match:

                first = elements[i]
                last = elements[i + len(words) - 1]

                return {
                    "x": (first["x"] + last["x"]) // 2,
                    "y": (first["y"] + last["y"]) // 2
                }

        return None

    # ---------------------------------------
    # Click visible text or semantic target
    # ---------------------------------------

    def click_text(self, phrase):
        if not phrase or not phrase.strip():
            self.last_error = "target not found"
            return False

        # Universal messaging input
        if phrase.lower() == "message_box":

            win = gw.getActiveWindow()

            if win is None:
                self.last_error = "target not found"
                return False

            x = win.left + win.width // 2
            y = win.top + int(win.height * 0.965)

            pyautogui.click(x, y)
            self.last_error = None
            return True

        # Normal OCR click
        try:
            item = self.find_text(phrase)
        except Exception as exc:
            self.last_error = "OCR failure"
            raise RuntimeError("OCR failure") from exc

        if item is None:
            self.last_error = "target not found"
            return False

        pyautogui.click(item["x"], item["y"])
        self.last_error = None
        return True

    # ---------------------------------------
    # Type into focused field
    # ---------------------------------------

    def type_text(self, text):
        pyautogui.write(text, interval=0.02)

    # ---------------------------------------
    # Header strip OCR (top of the active window)
    # ---------------------------------------
    #
    # Used to confirm WHICH conversation is open before sending anything.
    # Reading only the top strip keeps the contact name away from message
    # bodies and search results, which is exactly what a send guard needs.

    def read_header(self, frac=0.11, x_frac=0.30):
        """OCR the conversation header (top-right pane).

        The left column is the chat list and the search box, which contain
        OTHER people's names. Capturing only the right-hand conversation
        header keeps the guard from comparing against unrelated contacts.
        """

        win = gw.getActiveWindow()

        if win is None:
            return ""

        left = max(0, win.left + int(win.width * x_frac))
        top = max(0, win.top)
        width = max(1, win.width - int(win.width * x_frac))
        height = max(1, int(win.height * frac))

        try:
            image = pyautogui.screenshot(region=(left, top, width, height))
        except Exception:
            return ""

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name

        image.save(path)

        try:
            text = pytesseract.image_to_string(Image.open(path))
        except Exception:
            text = ""
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        return " ".join(text.split())

    # ---------------------------------------
    # Message composer OCR (bottom input row)
    # ---------------------------------------
    #
    # Tells a *sent* message apart from one still sitting in the input box:
    # after a real send the composer row is empty.

    def read_composer(self, frac=0.07, x_frac=0.30):
        """OCR the message input row at the bottom of the right-hand pane."""

        win = gw.getActiveWindow()

        if win is None:
            return ""

        left = max(0, win.left + int(win.width * x_frac))
        height = max(1, int(win.height * frac))
        top = max(0, win.top + win.height - height)
        width = max(1, win.width - int(win.width * x_frac))

        try:
            image = pyautogui.screenshot(region=(left, top, width, height))
        except Exception:
            return ""

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name

        image.save(path)

        try:
            text = pytesseract.image_to_string(Image.open(path))
        except Exception:
            text = ""
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        return " ".join(text.split())

    # ---------------------------------------
    # Active window geometry
    # ---------------------------------------

    def window_bounds(self):
        """Return ``(left, top, width, height)`` of the active window."""
        win = gw.getActiveWindow()
        if win is None:
            return None
        return (win.left, win.top, win.width, win.height)

    # ---------------------------------------
    # Sidebar rows (chat list / search results)
    # ---------------------------------------
    #
    # Used to resolve WHICH chat/search result the user means. WhatsApp's
    # search shows several near-identical names (person, contact card, groups
    # in common, another person entirely), so JARVIS needs the actual list of
    # visible candidates instead of blindly trusting the top hit.

    def read_sidebar_rows(
        self,
        x_frac: float = 0.02,
        width_frac: float = 0.28,
        top_frac: float = 0.09,
        bottom_frac: float = 0.88,
    ) -> list:
        """OCR the left pane into candidate rows: ``[{"name", "x", "y"}]``.

        Coordinates are absolute screen coordinates of the row's name line, so
        a caller can click a specific candidate to open that exact chat.
        """
        win = gw.getActiveWindow()
        if win is None:
            return []

        left = max(0, win.left + int(win.width * x_frac))
        top = max(0, win.top + int(win.height * top_frac))
        width = max(1, int(win.width * width_frac))
        height = max(1, int((bottom_frac - top_frac) * win.height))

        try:
            image = pyautogui.screenshot(region=(left, top, width, height))
        except Exception:
            return []

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name

        image.save(path)

        try:
            data = pytesseract.image_to_data(
                Image.open(path), output_type=pytesseract.Output.DICT
            )
        except Exception:
            return []
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        return self.lines_to_rows(data, offset=(left, top))

    @staticmethod
    def lines_to_rows(data: dict, offset=(0, 0)) -> list:
        """Group tesseract ``image_to_data`` output into named sidebar rows."""
        lines: dict = {}
        count = len(data.get("text", []))

        for i in range(count):
            token = (data["text"][i] or "").strip()
            if not token:
                continue
            try:
                conf = int(float(data["conf"][i]))
            except (TypeError, ValueError):
                conf = 0
            if conf < 40:
                continue

            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            lines.setdefault(key, []).append((
                data["left"][i],
                data["top"][i],
                data["width"][i],
                data["height"][i],
                token,
            ))

        ox, oy = offset
        rows = []
        for _key, toks in lines.items():
            toks.sort(key=lambda t: t[0])
            text = " ".join(t[4] for t in toks)
            x0 = min(t[0] for t in toks)
            y0 = min(t[1] for t in toks)
            x1 = max(t[0] + t[2] for t in toks)
            y1 = max(t[1] + t[3] for t in toks)
            name = clean_sidebar_name(text)
            if not name:
                continue
            rows.append({
                "name": name,
                "text": text,
                "x": ox + (x0 + x1) // 2,
                "y": oy + (y0 + y1) // 2,
            })

        rows.sort(key=lambda r: r["y"])
        return rows

    # ---------------------------------------
    # Whole-screen summary (window title + OCR text)
    # ---------------------------------------

    def read_screen(self):

        win = gw.getActiveWindow()
        window = win.title if win else None

        elements = self.read_elements()
        text = " ".join(e["text"] for e in elements)

        return {
            "window": window,
            "text": text
        }