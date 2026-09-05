import sys
from pathlib import Path

# Add the project root to Python path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from tools.vision import ScreenVision

vision = ScreenVision()

result = vision.read_screen()

print("=" * 40)
print("ACTIVE WINDOW")
print("=" * 40)
print(result["window"])

print("\n" + "=" * 40)
print("OCR TEXT")
print("=" * 40)
print(result["text"])