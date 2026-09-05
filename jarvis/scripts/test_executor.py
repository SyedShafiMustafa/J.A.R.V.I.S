import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from agents.planner import TaskPlanner
from tools.executor import TaskExecutor

planner = TaskPlanner()
executor = TaskExecutor()

plan = planner.create_plan(
    "Open Notepad and write Hello Shafi"
)

executor.execute(plan)