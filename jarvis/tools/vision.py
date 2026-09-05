import os
import tempfile

import pyautogui
import pygetwindow as gw
import pytesseract
from PIL import Image


class ScreenVision:

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
            except:
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

        # Universal messaging input
        if phrase.lower() == "message_box":

            win = gw.getActiveWindow()

            if win is None:
                return False

            x = win.left + win.width // 2
            y = win.top + int(win.height * 0.965)

            pyautogui.click(x, y)
            return True

        # Normal OCR click
        item = self.find_text(phrase)

        if item is None:
            return False

        pyautogui.click(item["x"], item["y"])
        return True

    # ---------------------------------------
    # Type into focused field
    # ---------------------------------------

    def type_text(self, text):
        pyautogui.write(text, interval=0.02)

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