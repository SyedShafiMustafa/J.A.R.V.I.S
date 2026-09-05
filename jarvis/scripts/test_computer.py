import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from tools.computer import ComputerController
from tools.desktop_control import DesktopController
import time

pc = ComputerController()
desktop = DesktopController()

desktop.open_app("notepad")

pc.wait_for_window("Notepad")

time.sleep(1)

pc.type_text("Hello Shafi!")
pc.press("enter")
pc.type_text("JARVIS now controls the keyboard.")