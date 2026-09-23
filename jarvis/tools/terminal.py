"""
tools/terminal.py

Terminal command execution tool for J.A.R.V.I.S.
Captures stdout, stderr, exit code, handles timeouts and process cancellation safely.
"""

from __future__ import annotations

import os
import subprocess
import logging
from pathlib import Path

from backend.interfaces import ToolResult
from config.settings import PROJECT_ROOT

_log = logging.getLogger("jarvis.terminal")


class TerminalTool:

    @staticmethod
    def execute_terminal(
        command: str,
        timeout: float = 30.0,
        cwd: str | None = None,
    ) -> ToolResult:
        if not command or not command.strip():
            return ToolResult("execute_terminal", False, "Command string cannot be empty", {"started": False, "completed": False})

        work_dir = Path(cwd).resolve() if cwd else PROJECT_ROOT

        _log.info("Executing terminal command: %s (cwd=%s, timeout=%s)", command, work_dir, timeout)

        try:
            res = subprocess.run(
                command,
                shell=True,
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            stdout = (res.stdout or "").strip()
            stderr = (res.stderr or "").strip()
            success = res.returncode == 0

            # The spoken message carries the actual output (first lines),
            # not just the exit code — "Run echo hi" should answer "hi".
            if success and stdout:
                first_lines = "\n".join(stdout.splitlines()[:5])[:300]
                message = f"Done — output:\n{first_lines}"
            elif success:
                message = "Done — no output."
            elif stderr:
                message = (f"Command failed (code {res.returncode}): "
                           f"{stderr.splitlines()[0][:200]}")
            else:
                message = f"Command exited with code {res.returncode}"

            return ToolResult(
                "execute_terminal",
                success,
                message,
                {
                    "returncode": res.returncode,
                    "stdout": stdout[:4000],
                    "stderr": stderr[:2000],
                    "started": True,
                    "completed": True,
                    "verified": success,
                },
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                "execute_terminal",
                False,
                f"Command timed out after {timeout} seconds",
                {"reason": "timeout", "started": True, "completed": False, "verified": False},
            )
        except Exception as exc:
            return ToolResult(
                "execute_terminal",
                False,
                f"Failed to execute command: {exc}",
                {"started": True, "completed": False, "verified": False},
            )
