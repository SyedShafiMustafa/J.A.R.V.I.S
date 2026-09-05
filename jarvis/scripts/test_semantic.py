import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from tools.vision import ScreenVision

vision = ScreenVision()

elements = vision.read_elements()

print(f"\nFound {len(elements)} text elements\n")

for e in elements[:40]:
    print(e)