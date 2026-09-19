"""
tools/git_tool.py

Git integration tools for J.A.R.V.I.S.
Includes status, diff, log, and branch inspection with automatic secret sanitization.
"""

from __future__ import annotations

import re
import subprocess
import logging

from backend.interfaces import ToolResult
from config.settings import PROJECT_ROOT

_log = logging.getLogger("jarvis.git")

_SECRET_PATTERN = re.compile(
    r"((?:api_key|secret|password|bearer|token)\s*=\s*)(['\"]?)[A-Za-z0-9_\-]{8,}\2",
    re.IGNORECASE,
)


def _sanitize_output(text: str) -> str:
    """Mask potential secret keys from git diff/log output."""
    if not text:
        return ""
    return _SECRET_PATTERN.sub(r"\1***REDACTED***", text)


class GitTool:

    @staticmethod
    def _run_git(args: list[str]) -> tuple[bool, str]:
        try:
            res = subprocess.run(
                ["git", *args],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=15.0,
            )
            out = res.stdout or res.stderr or ""
            return res.returncode == 0, _sanitize_output(out.strip())
        except Exception as exc:
            return False, str(exc)

    @classmethod
    def git_status(cls) -> ToolResult:
        success, output = cls._run_git(["status", "--short"])
        return ToolResult(
            "git_status",
            success,
            "Git status retrieved",
            {"status_output": output, "started": True, "completed": True, "verified": success},
        )

    @classmethod
    def git_diff(cls) -> ToolResult:
        success, output = cls._run_git(["diff"])
        return ToolResult(
            "git_diff",
            success,
            "Git diff retrieved",
            {"diff_output": output[:5000], "started": True, "completed": True, "verified": success},
        )

    @classmethod
    def git_log(cls, n: int = 5) -> ToolResult:
        success, output = cls._run_git(["log", f"-n{n}", "--oneline"])
        return ToolResult(
            "git_log",
            success,
            f"Git log (last {n} commits)",
            {"log_output": output, "started": True, "completed": True, "verified": success},
        )

    @classmethod
    def git_branch(cls) -> ToolResult:
        success, output = cls._run_git(["branch", "--show-current"])
        return ToolResult(
            "git_branch",
            success,
            f"Current branch: {output}",
            {"branch": output, "started": True, "completed": True, "verified": success},
        )
