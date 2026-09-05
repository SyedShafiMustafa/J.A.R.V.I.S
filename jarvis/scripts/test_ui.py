import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from tools.ui_automation import UIAutomation

ui = UIAutomation()

controls = ui.inspect()

print(f"\nFound {len(controls)} semantic controls\n")

for c in controls:
    print(c)