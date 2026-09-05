import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import time

from tools.desktop_control import DesktopController
from tools.computer import ComputerController

desktop = DesktopController()
pc = ComputerController()

print("🚀 Testing Desktop Controller...")

# Open Notepad
print("Opening Notepad...")
desktop.open_app("notepad")

pc.wait_for_window("Notepad")
time.sleep(1)

# Type text
print("Typing...")
pc.type_text("Hello from JARVIS v1.0!")
pc.press("enter")
pc.type_text("Desktop control is working perfectly.")

# Verify the active window
print("\nActive window:", pc.get_active_window())

print("✅ Test Complete")