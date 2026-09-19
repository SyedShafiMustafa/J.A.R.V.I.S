"""
tools/python_dev.py

Python development & test execution tools for J.A.R.V.I.S.
Execute scripts, run pytest, capture outputs and diagnostic failures.
"""

from __future__ import annotations

import sys
import subprocess
import logging
from pathlib import Path

from backend.interfaces import ToolResult
from config.settings import PROJECT_ROOT

_log = logging.getLogger("jarvis.pydev")


class PythonDevTools:

    @staticmethod
    def run_python_script(script_path: str, args: list[str] | None = None) -> ToolResult:
        path = Path(script_path).resolve()
        if not path.exists():
            return ToolResult("run_python_script", False, f"Script not found: {script_path}", {"started": False, "completed": False})

        cmd = [sys.executable, str(path), *(args or [])]
        try:
            res = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=60.0,
            )
            success = res.returncode == 0
            return ToolResult(
                "run_python_script",
                success,
                f"Script exited with code {res.returncode}",
                {
                    "returncode": res.returncode,
                    "stdout": (res.stdout or "").strip()[:4000],
                    "stderr": (res.stderr or "").strip()[:2000],
                    "started": True,
                    "completed": True,
                    "verified": success,
                },
            )
        except Exception as exc:
            return ToolResult("run_python_script", False, f"Execution failed: {exc}", {"started": True, "completed": False})

    @staticmethod
    def run_pytest(test_path: str | None = None) -> ToolResult:
        cmd = [sys.executable, "-m", "pytest", "-o", "pythonpath=."]
        if test_path:
            cmd.append(test_path)

        try:
            res = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=120.0,
            )
            success = res.returncode == 0
            return ToolResult(
                "run_pytest",
                success,
                f"Pytest exited with code {res.returncode}",
                {
                    "returncode": res.returncode,
                    "stdout": (res.stdout or "").strip()[:4000],
                    "stderr": (res.stderr or "").strip()[:2000],
                    "started": True,
                    "completed": True,
                    "verified": success,
                },
            )
        except Exception as exc:
            return ToolResult("run_pytest", False, f"Pytest execution failed: {exc}", {"started": True, "completed": False})
