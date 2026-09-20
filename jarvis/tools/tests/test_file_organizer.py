"""Tests for the autonomous file-management capability (tools/organizer.py).

Covers analysis, every plan strategy, collision-safe verified execution,
duplicate detection, natural-language parsing, permission classification,
executor/registry wiring, and the server's preview -> confirm flow.
"""

import os
import time
from pathlib import Path

import pytest

from backend.interfaces import ToolResult
from backend.tools import build_default_tool_registry
from core.permission import PermissionEngine, PermissionLevel
from tools.executor import TaskExecutor
from tools.organizer import (
    FileOrganizer,
    category_for,
    parse_organize_request,
    resolve_user_directory,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_tree(tmp_path: Path) -> Path:
    (tmp_path / "report.pdf").write_text("pdf")
    (tmp_path / "photo.jpg").write_text("img")
    (tmp_path / "notes.txt").write_text("txt")
    (tmp_path / "song.mp3").write_text("aud")
    (tmp_path / "archive.zip").write_text("zip")
    (tmp_path / "app.exe").write_text("exe")
    (tmp_path / "script.py").write_text("code")
    (tmp_path / "mystery.bin").write_text("other")
    return tmp_path


def _organizer_executor() -> TaskExecutor:
    instance = TaskExecutor.__new__(TaskExecutor)
    instance.organizer = FileOrganizer()
    return instance


def _run(executor: TaskExecutor, tool: str, **payload) -> ToolResult:
    step = dict(payload)
    step["tool"] = tool
    return executor.execute({"goal": tool, "steps": [step]})


# ---------------------------------------------------------------------------
# classification / resolution
# ---------------------------------------------------------------------------

def test_category_for_maps_common_types():
    assert category_for("a.pdf") == "Documents"
    assert category_for("a.JPG") == "Images"
    assert category_for("a.mp4") == "Video"
    assert category_for("a.py") == "Code"
    assert category_for("a.unknown") == "Other"


def test_resolve_user_directory_accepts_paths_and_names(tmp_path):
    assert resolve_user_directory(str(tmp_path)) == tmp_path.resolve()
    # A single-letter / nonsense name resolves to nothing (never a wrong folder).
    assert resolve_user_directory("zzz-nope") is None
    assert resolve_user_directory("") is None


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def test_analyze_directory_summarizes_without_changing_anything(tmp_path):
    _make_tree(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    result = _run(_organizer_executor(), "analyze_directory", directory=str(tmp_path))

    assert result.success and result.data["verified"]
    assert result.data["total_files"] == 8
    assert result.data["by_category"]["Documents"]["count"] == 2  # report.pdf, notes.txt
    assert result.data["by_category"]["Images"]["count"] == 1
    assert result.data["by_category"]["Other"]["count"] == 1
    # read-only: files untouched
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_analyze_directory_missing_folder_is_honest(tmp_path):
    result = _run(_organizer_executor(), "analyze_directory", directory="definitely-not-here")
    assert not result.success
    assert "couldn't find" in result.message.lower()


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

def test_build_plan_by_type_maps_each_file(tmp_path):
    _make_tree(tmp_path)
    plan = FileOrganizer().build_plan(str(tmp_path), "by_type")
    moves = {Path(m["src"]).name: m["category"] for m in plan["moves"]}
    assert moves["report.pdf"] == "Documents"
    assert moves["photo.jpg"] == "Images"
    assert moves["song.mp3"] == "Audio"
    assert moves["archive.zip"] == "Archives"
    assert moves["app.exe"] == "Installers"
    assert moves["script.py"] == "Code"
    assert moves["mystery.bin"] == "Other"


def test_build_plan_by_extension_uses_extension_folder(tmp_path):
    _make_tree(tmp_path)
    plan = FileOrganizer().build_plan(str(tmp_path), "by_extension")
    categories = {Path(m["src"]).name: m["category"] for m in plan["moves"]}
    assert categories["report.pdf"] == "pdf"
    assert categories["script.py"] == "py"


def test_build_plan_by_date_uses_year_month(tmp_path):
    _make_tree(tmp_path)
    old = tmp_path / "old.txt"
    old.write_text("old")
    stamp = time.mktime((2021, 3, 15, 12, 0, 0, 0, 0, -1))
    os.utime(old, (stamp, stamp))

    plan = FileOrganizer().build_plan(str(tmp_path), "by_date")
    folders = {Path(m["src"]).name: m["category"] for m in plan["moves"]}
    assert folders["old.txt"] == "2021/03"


def test_build_plan_by_age_only_includes_old_files(tmp_path):
    fresh = tmp_path / "fresh.txt"
    fresh.write_text("fresh")
    stale = tmp_path / "stale.txt"
    stale.write_text("stale")
    old = time.time() - 200 * 86400
    os.utime(stale, (old, old))

    plan = FileOrganizer().build_plan(str(tmp_path), "by_age", older_than_days=90)
    names = {Path(m["src"]).name for m in plan["moves"]}
    assert names == {"stale.txt"}
    assert plan["moves"][0]["category"].startswith("Archive/")
    skipped = {Path(s["path"]).name for s in plan["skipped"]}
    assert "fresh.txt" in skipped


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

def test_dry_run_previews_without_moving(tmp_path):
    _make_tree(tmp_path)
    result = _run(
        _organizer_executor(),
        "organize_directory",
        directory=str(tmp_path),
        dry_run=True,
    )
    assert result.success
    assert result.data["dry_run"] is True
    assert result.data["moves"] == 8
    assert (tmp_path / "report.pdf").exists()  # untouched
    assert not (tmp_path / "Documents").exists()


def test_execute_moves_and_verifies(tmp_path):
    _make_tree(tmp_path)
    result = _run(
        _organizer_executor(),
        "organize_directory",
        directory=str(tmp_path),
        dry_run=False,
    )
    assert result.success and result.data["verified"]
    assert (tmp_path / "Documents" / "report.pdf").exists()
    assert (tmp_path / "Images" / "photo.jpg").exists()
    assert not (tmp_path / "report.pdf").exists()
    assert result.data["moves"] == 8


def test_collision_is_never_overwritten(tmp_path):
    _make_tree(tmp_path)
    # A file already in the destination with different content.
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "report.pdf").write_text("ORIGINAL")

    result = _run(
        _organizer_executor(),
        "organize_directory",
        directory=str(tmp_path),
        dry_run=False,
    )
    assert result.success
    assert (tmp_path / "Documents" / "report.pdf").read_text() == "ORIGINAL"
    assert (tmp_path / "Documents" / "report (1).pdf").read_text() == "pdf"


def test_organize_is_idempotent_when_already_tidy(tmp_path):
    _make_tree(tmp_path)
    _run(_organizer_executor(), "organize_directory", directory=str(tmp_path), dry_run=False)
    second = _run(
        _organizer_executor(),
        "organize_directory",
        directory=str(tmp_path),
        dry_run=False,
    )
    assert second.success
    assert second.data["moves"] == 0
    assert "already tidy" in second.message.lower()


# ---------------------------------------------------------------------------
# duplicates
# ---------------------------------------------------------------------------

def test_find_duplicates_groups_identical_content(tmp_path):
    (tmp_path / "one.txt").write_text("same-content")
    (tmp_path / "two.txt").write_text("same-content")
    (tmp_path / "other.txt").write_text("different")

    result = _run(_organizer_executor(), "find_duplicates", directory=str(tmp_path))
    assert result.success
    assert result.data["group_count"] == 1
    files = result.data["groups"][0]["files"]
    assert len(files) == 2
    assert result.data["wasted_bytes"] == len("same-content")


# ---------------------------------------------------------------------------
# natural-language parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("organize my downloads", ("downloads", "by_type")),
        ("can you sort my downloads folder by type?", ("downloads", "by_type")),
        ("tidy up the desktop", ("desktop", "by_type")),
        ("clean up my documents by date", ("documents", "by_date")),
        ("organize my pictures by year", ("pictures", "by_year")),
        ("sort my downloads by extension", ("downloads", "by_extension")),
    ],
)
def test_parse_organize_request(text, expected):
    parsed = parse_organize_request(text)
    assert parsed is not None
    assert (parsed["directory"], parsed["strategy"]) == expected


def test_parse_organize_age_threshold():
    parsed = parse_organize_request("archive files older than 6 months in downloads")
    assert parsed["strategy"] == "by_age"
    assert parsed["older_than_days"] == 180


def test_parse_organize_explicit_skips_confirmation():
    assert parse_organize_request("organize my downloads and go ahead")["explicit"] is True
    assert parse_organize_request("organize my downloads")["explicit"] is False


def test_parse_ignores_non_file_requests():
    assert parse_organize_request("what is the weather") is None
    assert parse_organize_request("open notepad") is None


# ---------------------------------------------------------------------------
# permission classification
# ---------------------------------------------------------------------------

def test_permission_of_organize_depends_on_dry_run():
    engine = PermissionEngine()
    harmless, needs_preview = engine.evaluate("organize_directory", {"dry_run": True})
    assert harmless == PermissionLevel.HARMLESS and needs_preview is False

    normal, needs_execute = engine.evaluate("organize_directory", {"dry_run": False})
    assert normal == PermissionLevel.NORMAL and needs_execute is False
    # The engine never gates a normal action; the yes/no confirmation for a
    # batch move is enforced by the server's preview -> confirm flow instead.
    _, gated = engine.evaluate("organize_directory", {"dry_run": False}, is_explicit_user_request=False)
    assert gated is False


def test_permission_read_only_tools_are_harmless():
    engine = PermissionEngine()
    for tool in ("analyze_directory", "find_duplicates"):
        level, requires = engine.evaluate(tool, {})
        assert level == PermissionLevel.HARMLESS
        assert requires is False


# ---------------------------------------------------------------------------
# router / classifier / planner wiring
# ---------------------------------------------------------------------------

def test_router_defers_file_management_instead_of_opening_an_app():
    from core.router import CommandRouter

    handled, reply = CommandRouter().route("organize my downloads")
    assert handled is False
    assert reply is None


def test_action_classifier_recognizes_file_management():
    from backend.live_adapters import _is_action_request

    for text in ("organize my downloads", "sort downloads", "find duplicates", "clean up my desktop"):
        assert _is_action_request(text) is True, text


def test_planner_accepts_organizer_tools():
    from agents.planner import TaskPlanner

    TaskPlanner._validate_plan({
        "goal": "organize downloads",
        "steps": [
            {"tool": "analyze_directory", "directory": "downloads"},
            {"tool": "organize_directory", "directory": "downloads", "strategy": "by_type", "dry_run": False},
            {"tool": "find_duplicates", "directory": "downloads", "recursive": True},
        ],
    })


# ---------------------------------------------------------------------------
# registry / executor wiring
# ---------------------------------------------------------------------------

def test_registry_exposes_new_tools():
    registry = build_default_tool_registry()
    for tool in ("analyze_directory", "find_duplicates", "organize_directory"):
        assert registry.get(tool) is not None, tool
    assert registry.get("organize_directory").supports_dry_run is True


# ---------------------------------------------------------------------------
# server preview -> confirm flow
# ---------------------------------------------------------------------------

class _FakeToolRunner:
    def __init__(self, directory):
        self.calls = []
        self.directory = directory

    def run(self, call, task=None, confirmed=False):
        self.calls.append((call.tool, dict(call.payload)))
        if call.tool == "organize_directory":
            dry = call.payload.get("dry_run", True)
            if dry:
                return ToolResult(call.tool, True, "I can organize 3 files.", {"moves": 3, "dry_run": True})
            return ToolResult(call.tool, True, "Organized 3 files.", {"moves": 3, "verified": True})
        return ToolResult(call.tool, True, "ok", {})


class _Audio:
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)


def _service_with_runtime(tmp_path, monkeypatch):
    from backend.server import JarvisBackendService

    monkeypatch.setattr(
        "tools.organizer.resolve_user_directory",
        lambda value, _t=tmp_path: _t,
    )
    runner = _FakeToolRunner(tmp_path)
    audio = _Audio()
    service = JarvisBackendService()
    events = []
    monkeypatch.setattr(service, "emit", lambda payload: events.append(payload))
    runtime = {"tool_runner": runner, "audio": audio, "memory": None}
    return service, runtime, runner, audio, events


def test_server_organize_previews_and_asks_before_moving(tmp_path, monkeypatch):
    service, runtime, runner, audio, events = _service_with_runtime(tmp_path, monkeypatch)

    handled = service._handle_organize_command(runtime, "organize my downloads")
    assert handled is True
    assert runner.calls[0][0] == "organize_directory"
    assert runner.calls[0][1]["dry_run"] is True
    assert any(e.get("type") == "confirmation_required" for e in events)
    assert service._pending_organize is not None
    # No execution call yet — only the preview.
    assert len(runner.calls) == 1


def test_server_organize_confirm_executes(tmp_path, monkeypatch):
    service, runtime, runner, audio, events = _service_with_runtime(tmp_path, monkeypatch)
    service._handle_organize_command(runtime, "organize my downloads")

    assert service._answer_organize(runtime, "yes go ahead") is True
    assert service._pending_organize is None
    assert runner.calls[-1][1]["dry_run"] is False
    assert any(e.get("type") == "organize_done" for e in events)
    assert any("Organized" in line for line in audio.spoken)


def test_server_organize_cancel_does_not_move(tmp_path, monkeypatch):
    service, runtime, runner, audio, events = _service_with_runtime(tmp_path, monkeypatch)
    service._handle_organize_command(runtime, "organize my downloads")

    assert service._answer_organize(runtime, "no") is True
    assert service._pending_organize is None
    assert len(runner.calls) == 1  # only the preview
    assert any(e.get("type") == "organize_cancelled" for e in events)


def test_server_organize_unrelated_utterance_drops_question(tmp_path, monkeypatch):
    service, runtime, runner, audio, events = _service_with_runtime(tmp_path, monkeypatch)
    service._handle_organize_command(runtime, "organize my downloads")

    assert service._answer_organize(runtime, "what is the capital of France") is False
    assert service._pending_organize is None
    assert len(runner.calls) == 1
