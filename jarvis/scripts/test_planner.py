import sys
from pathlib import Path
import json

# Add project root to Python path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from agents.planner import TaskPlanner

planner = TaskPlanner()

plan = planner.create_plan(
    "Open WhatsApp and message Mumma Hello"
)

print(json.dumps(plan, indent=2))