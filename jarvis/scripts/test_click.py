import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from tools.vision import ScreenVision

vision = ScreenVision()

print("You have 5 seconds...")
import time
time.sleep(5)

if vision.click_text("Explorer"):
    print("Clicked Explorer")
else:
    print("Text not found")