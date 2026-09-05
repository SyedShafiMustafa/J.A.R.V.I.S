from pywinauto.application import Application
import win32gui
import pyautogui


class UIAutomation:

    def __init__(self):
        pass

    # -------------------------------
    # Active foreground window
    # -------------------------------

    def active_window(self):
        hwnd = win32gui.GetForegroundWindow()
        app = Application(backend="uia").connect(handle=hwnd)
        return app.window(handle=hwnd)

    # -------------------------------
    # Get meaningful controls only
    # -------------------------------

    def inspect(self):

        window = self.active_window()

        controls = []
        seen = set()

        for c in window.descendants():

            try:
                name = c.window_text().strip()

                if not name:
                    continue

                ctype = c.element_info.control_type

                if ctype not in [
                    "Button",
                    "Edit",
                    "Text",
                    "Document",
                    "ListItem"
                ]:
                    continue

                key = (name, ctype)

                if key in seen:
                    continue

                seen.add(key)

                rect = c.rectangle()

                controls.append({
                    "name": name,
                    "type": ctype,
                    "left": rect.left,
                    "top": rect.top,
                    "right": rect.right,
                    "bottom": rect.bottom
                })

            except:
                pass

        return controls

    # -------------------------------
    # Find by visible text
    # -------------------------------

    def find(self, text):

        text = text.lower()

        for item in self.inspect():

            if text in item["name"].lower():
                return item

        return None

    # -------------------------------
    # Click semantic element
    # -------------------------------

    def click(self, text):

        item = self.find(text)

        if not item:
            return False

        x = (item["left"] + item["right"]) // 2
        y = (item["top"] + item["bottom"]) // 2

        pyautogui.click(x, y)

        return True

    # -------------------------------
    # Type into focused field
    # -------------------------------

    def type(self, text):
        pyautogui.write(text, interval=0.02)