import time
from tools.desktop_control import DesktopController

desktop = DesktopController()

print("🚀 Testing Desktop Controller...")

# Open Notepad
print("Opening Notepad...")
desktop.open_app("notepad")

time.sleep(2)

# Type text
print("Typing...")
desktop.type_text("Hello from JARVIS v1.0!")
desktop.press("enter")
desktop.type_text("Desktop control is working perfectly.")

# List open windows
print("\nOpen Windows:")
for window in desktop.list_windows()[:10]:
    print("-", window)

# Take screenshot
path = desktop.screenshot()

print(f"\nScreenshot saved to: {path}")
print("✅ Test Complete")