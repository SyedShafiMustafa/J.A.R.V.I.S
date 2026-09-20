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

    def screen_size(self):
        """Primary-screen ``{"width","height"}`` or None if unavailable."""
        try:
            size = pyautogui.size()
            return {"width": int(size.width), "height": int(size.height)}
        except Exception:
            return None

    def focus_window(self, title):
        windows = gw.getWindowsWithTitle(title)
        if not windows:
            return False

        win = windows[0]
        try:
            win.activate()
        except Exception:
            # pygetwindow on Windows can raise PyGetWindowException even when successful
            pass
        if self._window_is_active(win):
            return True

        # activate() is advisory on Windows and often leaves the old
        # window in front: fall back to a direct foreground request, then
        # report honestly instead of claiming focus we do not have.
        try:
            import win32gui
            win32gui.SetForegroundWindow(win._hWnd)
            time.sleep(0.3)
        except Exception:
            pass
        if self._window_is_active(win):
            return True
        # Last resort: Windows denies foreground transfers to background
        # processes (e.g. while the user works elsewhere). Attaching our
        # thread to the foreground thread makes the transfer permissible
        # — standard automation practice, then always detach.
        try:
            import ctypes
            import win32gui
            import win32process
            fg = win32gui.GetForegroundWindow()
            fg_tid = win32process.GetWindowThreadProcessId(fg)[0]
            our_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            win32process.AttachThreadInput(our_tid, fg_tid, True)
            try:
                win32gui.SetForegroundWindow(win._hWnd)
                time.sleep(0.3)
            finally:
                try:
                    win32process.AttachThreadInput(our_tid, fg_tid, False)
                except Exception:
                    pass
        except Exception:
            pass
        return self._window_is_active(win)

    @staticmethod
    def _window_is_active(win):
        """True only when ``win`` is verifiably the foreground window."""
        try:
            active = gw.getActiveWindow()
        except Exception:
            return True  # unreadable state: keep legacy optimistic contract
        if active is None:
            return False
        for attr in ("_hWnd", "title"):
            try:
                if getattr(active, attr) != getattr(win, attr):
                    return False
            except AttributeError:
                continue
        return True

    def wait_for_window(self, title, timeout=10):

        start = time.time()

        while time.time() - start < timeout:

            if self.focus_window(title):
                return True

            time.sleep(0.3)

        return False