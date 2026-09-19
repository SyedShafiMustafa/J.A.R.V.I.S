"""
tests/test_computer_foundation.py

Unit tests for filesystem, terminal, git, and python dev tools.
"""

import tempfile
from pathlib import Path
import pytest

from tools.filesystem import FilesystemTools
from tools.terminal import TerminalTool
from tools.git_tool import GitTool, _sanitize_output
from tools.python_dev import PythonDevTools


def test_filesystem_crud():
    with tempfile.TemporaryDirectory() as tmp_dir:
        folder = Path(tmp_dir)
        file_path = str(folder / "hello.txt")

        # 1. Create file
        res_create = FilesystemTools.create_file(file_path, "Hello World")
        assert res_create.success is True
        assert res_create.data["verified"] is True

        # 2. Inspect file
        res_inspect = FilesystemTools.inspect_file(file_path)
        assert res_inspect.success is True
        assert res_inspect.data["content"] == "Hello World"

        # 3. Edit file
        res_edit = FilesystemTools.edit_file(file_path, "Hello JARVIS")
        assert res_edit.success is True
        assert res_edit.data["verified"] is True

        # 4. List files
        res_list = FilesystemTools.list_files(str(folder))
        assert res_list.success is True
        assert len(res_list.data["items"]) == 1
        assert res_list.data["items"][0]["name"] == "hello.txt"

        # 5. Search files
        res_search = FilesystemTools.search_files(str(folder), "*.txt")
        assert res_search.success is True
        assert len(res_search.data["matches"]) == 1

        # 6. Delete file
        res_del = FilesystemTools.delete_file(file_path)
        assert res_del.success is True
        assert res_del.data["verified"] is True


def test_terminal_tool():
    res = TerminalTool.execute_terminal("echo 'Hello Terminal'")
    assert res.success is True
    assert "Hello Terminal" in res.data["stdout"] or "Hello" in res.data["stdout"]


def test_git_tool_sanitization():
    raw_secret = "api_key = 'sk-1234567890abcdef'"
    sanitized = _sanitize_output(raw_secret)
    assert "sk-1234567890abcdef" not in sanitized
    assert "REDACTED" in sanitized


def test_git_tool_execution():
    res = GitTool.git_status()
    assert res.success is True
    assert res.tool == "git_status"


def test_executor_routes_new_tools(monkeypatch, tmp_path):
    """TaskExecutor must dispatch the new tools with correct argument names."""
    from tools.executor import TaskExecutor
    from backend.interfaces import ToolResult

    executor = TaskExecutor()

    seen = {}

    def fake_run_pytest(test_path=None):
        seen["test_path"] = test_path
        return ToolResult("run_pytest", True, "ok", {"started": True, "completed": True})

    monkeypatch.setattr(executor.pydev, "run_pytest", fake_run_pytest)
    res = executor.execute({"goal": "t", "steps": [{"tool": "run_pytest", "test_path": "core/tests"}]})
    assert res.success is True
    assert seen["test_path"] == "core/tests"

    target = tmp_path / "routed.txt"
    res = executor.execute({"goal": "c", "steps": [{"tool": "create_file", "path": str(target), "content": "hi"}]})
    assert res.success is True
    assert target.read_text(encoding="utf-8") == "hi"

    # send_whatsapp without a recipient/message fails honestly instead of KeyError.
    res = executor.execute({"goal": "w", "steps": [{"tool": "send_whatsapp", "recipient": "", "message": ""}]})
    assert res.success is False
    assert "recipient" in res.message.lower()


def test_executor_preserves_final_step_result():
    """The final step's tool-specific data (stdout/verified) must survive,
    not be collapsed into a generic 'Task completed'."""
    from tools.executor import TaskExecutor

    res = TaskExecutor().execute(
        {"goal": "t", "steps": [{"tool": "execute_terminal", "command": "echo propagation-check"}]}
    )
    assert res.success is True
    assert res.tool == "execute_terminal"
    assert "propagation-check" in (res.data or {}).get("stdout", "")
