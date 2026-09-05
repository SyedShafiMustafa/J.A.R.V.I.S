import time
import pyautogui
import pyperclip
import pygetwindow as gw

# Safety: moving mouse to top-left aborts automation
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.15


class ComputerController:

    # ----------------------------
    # Mouse
    # ----------------------------

    def move(self, x: int, y: int, duration=0.2):
        pyautogui.moveTo(x, y, duration=duration)

    def click(self, x=None, y=None):
        pyautogui.click(x=x, y=y)

    def right_click(self, x=None, y=None):
        pyautogui.rightClick(x=x, y=y)

    def double_click(self, x=None, y=None):
        pyautogui.doubleClick(x=x, y=y)

    def drag(self, x, y, duration=0.4):
        pyautogui.dragTo(x, y, duration=duration, button="left")

    def scroll(self, amount):
        pyautogui.scroll(amount)

    # ----------------------------
    # Keyboard
    # ----------------------------

    def type_text(self, text, interval=0.02):
        pyautogui.write(text, interval=interval)

    def press(self, key):
        pyautogui.press(key)

    def hotkey(self, *keys):
        pyautogui.hotkey(*keys)

    # ----------------------------
    # Clipboard
    # ----------------------------

    def copy(self):
        pyautogui.hotkey("ctrl", "c")

    def paste(self):
        pyautogui.hotkey("ctrl", "v")

    def set_clipboard(self, text):
        pyperclip.copy(text)

    def get_clipboard(self):
        return pyperclip.paste()

    # ----------------------------
    # Windows
    # ----------------------------

    def get_active_window(self):
        win = gw.getActiveWindow()

        if win:
            return win.title

        return None

    def focus_window(self, title):

        windows = gw.getWindowsWithTitle(title)

        if windows:
            windows[0].activate()
            return True

        return False

    def wait_for_window(self, title, timeout=10):

        start = time.time()

        while time.time() - start < timeout:

            if self.focus_window(title):
                return True

            time.sleep(0.3)

        return False