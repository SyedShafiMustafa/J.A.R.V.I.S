"""§30 winget package management — unit/integration/regression tests.

Covers the spec §17 matrix with a fake winget backend (no real
winget touched): parsing, exact/unique/ambiguous resolution, command
construction, stdout parsing, stderr handling, exit codes, timeout,
permission integration, action-vs-explanation, install/upgrade/
uninstall verification, partial bulk-update failure, experience
recording and planner/executor/registry wiring.
"""

import pytest

from agents.ollama_errors import PlannerValidationError
from agents.planner import TaskPlanner
from backend.live_adapters import _is_action_request
from backend.tools import build_default_tool_registry
from core.intent import classify
from core.permission import PermissionEngine, PermissionLevel
from core.router import CommandRouter
from tools.executor import TaskExecutor
from tools.packages import (
    NOT_FOUND_CODE,
    PackageController,
    PackageResult,
    WingetBackend,
    _parse_table,
    parse_package_command,
    resolve_installed,
    resolve_remote,
)


# ---------------------------------------------------------------------------
# Fake backend (scripted winget argv → result)
# ---------------------------------------------------------------------------

def _table(rows, cols=("Name", "Id", "Version", "Source")):
    widths = {c: max(len(c), *(len(r.get(c.lower(), "")) for r in rows))
              for c in cols}
    header = " ".join(c.ljust(widths[c]) for c in cols).rstrip()
    dashes = " ".join("-" * widths[c] for c in cols)
    lines = [header, dashes]
    for row in rows:
        lines.append(" ".join(row.get(c.lower(), "").ljust(widths[c])
                              for c in cols).rstrip())
    return "\n".join(lines) + "\n"


_GIT_SEARCH = _table([
    {"name": "Git", "id": "Git.Git", "version": "2.55.0",
     "source": "winget"},
    {"name": "Git", "id": "Microsoft.Git", "version": "2.55.0",
     "source": "winget"},
])
_GIT_INSTALLED = _table(
    [{"name": "Git", "id": "Git.Git", "version": "2.45.1",
      "available": "2.55.0", "source": "winget"}],
    cols=("Name", "Id", "Version", "Available", "Source"))
_PY_SEARCH = _table([
    {"name": "Python 3.12", "id": "Python.Python.3.12",
     "version": "3.12.0", "source": "winget"},
    {"name": "Python 3.13", "id": "Python.Python.3.13",
     "version": "3.13.0", "source": "winget"},
])


class _FakeWinget(WingetBackend):
    """Scripted argv matcher. Keys are (verb, needle) tuples."""

    def __init__(self, script, installed=None):
        super().__init__(runner=self._fake_run)
        self.script = script
        self.argv_log: list[list[str]] = []
        self.state = dict(installed or {})
        self.no_shell = True

    def available(self):
        return True

    def _fake_run(self, argv, timeout):
        assert isinstance(argv, list), "structured argv required"
        assert "shell" not in argv and ";" not in " ".join(argv)
        self.argv_log.append(list(argv))
        verb = argv[1] if len(argv) > 1 else ""
        blob = " ".join(argv)
        for key_verb, needle, result in self.script:
            if verb == key_verb and needle in blob:
                return dict(result)
        raise AssertionError(f"unstubbed winget call: {argv}")


def _ok(stdout="", exit_code=0):
    return {"ok": exit_code == 0, "exit_code": exit_code,
            "stdout": stdout, "stderr": "", "duration_ms": 5.0}


def _ctl(script, installed=None):
    return PackageController(_FakeWinget(script, installed))


# ---------------------------------------------------------------------------
# Parsing (§7): action forms route, how-to forms do not
# ---------------------------------------------------------------------------

_PARSE_ACTION = [
    ("Install Git.", "package_install", {"package": "git"}),
    ("Please install Git.", "package_install", {"package": "git"}),
    ("Can you install Git for me?", "package_install",
     {"package": "git"}),
    ("Update Git.", "package_upgrade", {"package": "git"}),
    ("Uninstall Git.", "package_uninstall", {"package": "git"}),
    ("Remove the Git app.", "package_uninstall", {"package": "git"}),
    ("Search for Git.", "package_search", {"package": "git"}),
    ("Is Git installed?", "package_inspect", {"package": "git"}),
    ("What version of Git is installed?", "package_inspect",
     {"package": "git"}),
    ("Show me the installed version of Python.", "package_inspect",
     {"package": "python"}),
    ("Show installed packages.", "package_inspect", {}),
    ("Update all my winget packages.", "package_upgrade_all", {}),
    ("Upgrade all packages.", "package_upgrade_all", {}),
]

_PARSE_NONE = [
    "How do I install Git?",
    "How can I update Git?",
    "Search Google for Python.",
    "Search my files.",
    "What time is it?",
    "Is dark mode on?",
    "",
]


@pytest.mark.parametrize("text,tool,payload", _PARSE_ACTION)
def test_parse_action_forms(text, tool, payload):
    assert parse_package_command(text) == (tool, payload)


@pytest.mark.parametrize("text", _PARSE_NONE)
def test_parse_non_package_returns_none(text):
    assert parse_package_command(text) is None


# ---------------------------------------------------------------------------
# Action gate (§7): act for commands (incl. verb-less bulk), explain/chat else
# ---------------------------------------------------------------------------

_GATE_ACT = [
    "Install Git.",
    "Please install Git.",
    "Can you install Git for me?",
    "Update Git.",
    "Uninstall Git.",
    "Is Git installed?",
    "Search for Git.",
    "Update all my winget packages.",
    "Upgrade all packages.",
]

_GATE_NOT_ACT = [
    "How do I install Git?",
    "How can I update Git?",
    "What time is it?",
]


@pytest.mark.parametrize("text", _GATE_ACT)
def test_package_commands_reach_action(text):
    assert _is_action_request(text) is True, f"fell through: {text!r}"


@pytest.mark.parametrize("text", _GATE_NOT_ACT)
def test_package_howto_never_acts(text):
    assert _is_action_request(text) is False, f"acted: {text!r}"
    assert classify(text) != "act" or "winget" not in text.lower()


def test_howto_still_explains():
    assert classify("How do I install Git?") == "explain"
    assert classify("How can I update Git?") == "explain"


# ---------------------------------------------------------------------------
# Router: package phrases defer, never launch
# ---------------------------------------------------------------------------

class _Desktop:
    def __init__(self):
        self.opened = []

    def open_app(self, app):
        self.opened.append(app)
        return True

    def close_app(self, app):
        return False


class _Computer:
    def wait_for_window(self, title, timeout=8.0):
        return True


@pytest.mark.parametrize("text", _GATE_ACT)
def test_router_never_launches_for_packages(monkeypatch, text):
    import core.router as router_mod
    desktop = _Desktop()
    monkeypatch.setattr(router_mod, "desktop", desktop)
    monkeypatch.setattr(router_mod, "computer", _Computer())
    handled, _ = CommandRouter().route(text)
    assert desktop.opened == [], f"{text!r} launched an app"
    assert handled is False, f"{text!r} must defer to the planner"


# ---------------------------------------------------------------------------
# Table parsing: header-label columns, not-found, garbage
# ---------------------------------------------------------------------------

def test_parse_search_table():
    rows = _parse_table(_GIT_SEARCH)
    assert len(rows) == 2
    assert rows[0]["name"] == "Git"
    assert rows[0]["id"] == "Git.Git"
    assert rows[0]["version"] == "2.55.0"
    assert rows[0]["source"] == "winget"


def test_parse_installed_table_with_available():
    rows = _parse_table(_GIT_INSTALLED)
    assert len(rows) == 1
    assert rows[0]["id"] == "Git.Git"
    assert rows[0]["available"] == "2.55.0"


def test_parse_garbage_returns_empty():
    assert _parse_table("") == []
    assert _parse_table("No package found matching input criteria.") == []
    assert _parse_table("Name\n----\n") == []


# ---------------------------------------------------------------------------
# Resolution (§5): exact → known → unique → ambiguous (never a guess)
# ---------------------------------------------------------------------------

def test_resolve_exact_id():
    rows = _parse_table(_GIT_SEARCH)
    hit = resolve_remote("git.git", rows, "Git.Git")
    assert hit == {"ok": True, "package_id": "Git.Git",
                   "row": rows[0], "strategy": "exact_id"}


def test_resolve_known_id_when_names_collide():
    rows = _parse_table(_GIT_SEARCH)
    hit = resolve_remote("git", rows, "Git.Git")
    assert hit["ok"] is False
    assert hit["reason"] == "ambiguous_package"
    assert {c["id"] for c in hit["candidates"]} == {
        "Git.Git", "Microsoft.Git"}


def test_resolve_unique_match():
    rows = [{"name": "Solo Tool", "id": "Solo.Solo", "version": "1.0"}]
    hit = resolve_remote("solo", rows, None)
    assert hit["package_id"] == "Solo.Solo"
    assert hit["strategy"] == "unique_match"


def test_resolve_empty_is_not_found():
    assert resolve_remote("nope", [], None) == {
        "ok": False, "reason": "package_not_found"}


def test_resolve_installed_exact_and_ambiguous():
    rows = _parse_table(_GIT_INSTALLED)
    hit = resolve_installed("git", "Git.Git", rows)
    assert hit["ok"] is True
    assert hit["strategy"] == "exact_id"
    assert resolve_installed("nope", None, []) == {
        "ok": False, "reason": "not_installed"}


# ---------------------------------------------------------------------------
# SEARCH / INSPECT (harmless reads)
# ---------------------------------------------------------------------------

def test_search_lists_candidates():
    ctl = _ctl([("search", "Git", _ok(_GIT_SEARCH))])
    res = ctl.search("Git")
    assert res.success and res.verified
    assert "Git.Git" in res.message
    assert res.evidence["count"] == 2


def test_search_no_match_is_honest():
    ctl = _ctl([("search", "Nope",
                 _ok("No package found matching input criteria.",
                     NOT_FOUND_CODE))])
    res = ctl.search("NopeXYZ")
    assert res.success and res.verified
    assert "couldn't find" in res.message.lower()


def test_search_timeout_is_structured():
    backend = _FakeWinget([])
    backend._runner = lambda argv, timeout: {
        "ok": False, "exit_code": None, "stdout": "", "stderr": "",
        "duration_ms": 1.0, "reason": "timeout"}
    res = PackageController(backend).search("Git")
    assert not res.success and res.error == "timeout"


def test_inspect_reports_version_and_update():
    ctl = _ctl([("list", "Git", _ok(_GIT_INSTALLED))])
    res = ctl.inspect("Git")
    assert res.success and res.verified
    assert res.new_version == "2.45.1"
    assert "2.55.0" in res.message


def test_inspect_missing_is_honest():
    ctl = _ctl([("list", "Nope",
                 _ok("No installed package found matching input "
                     "criteria.", NOT_FOUND_CODE))])
    res = ctl.inspect("NopeXYZ")
    assert res.success and res.verified
    assert "not installed" in res.message.lower()


def test_inspect_all_enumerates():
    ctl = _ctl([("list", "winget", _ok(_GIT_INSTALLED))])
    res = ctl.inspect()
    assert res.success and res.verified
    assert res.evidence["count"] == 1


# ---------------------------------------------------------------------------
# INSTALL (§8): already-installed, verify, ambiguity, failure
# ---------------------------------------------------------------------------

def test_install_already_present_changes_nothing():
    ctl = _ctl([("list", "Git", _ok(_GIT_INSTALLED))])
    res = ctl.install("Git")
    assert res.success and res.verified
    assert res.evidence["already_installed"] is True
    assert "already installed" in res.message.lower()


def _installed_table(name, pid, version, available=""):
    return _table(
        [{"name": name, "id": pid, "version": version,
          "available": available, "source": "winget"}],
        cols=("Name", "Id", "Version", "Available", "Source"))


def _not_found(text="No package found matching input criteria."):
    return {"ok": False, "exit_code": NOT_FOUND_CODE, "stdout": text,
            "stderr": "", "duration_ms": 5.0}


def test_install_verifies_presence():
    state = {"installed": False}
    solo_search = _table([{"name": "Solo", "id": "Solo.Solo",
                           "version": "1.0", "source": "winget"}])

    def _runner(argv, timeout):
        verb = argv[1]
        if verb == "list":
            if state["installed"]:
                out = _installed_table("Solo", "Solo.Solo", "1.0")
                return {"ok": True, "exit_code": 0, "stdout": out,
                        "stderr": "", "duration_ms": 1.0}
            return _not_found("No installed package found matching "
                              "input criteria.")
        if verb == "search":
            return {"ok": True, "exit_code": 0, "stdout": solo_search,
                    "stderr": "", "duration_ms": 1.0}
        if verb == "install":
            assert argv[2:5] == ["--exact", "--id", "Solo.Solo"], argv
            assert "--silent" in argv and "--disable-interactivity" \
                in argv
            state["installed"] = True
            return {"ok": True, "exit_code": 0,
                    "stdout": "Successfully installed", "stderr": "",
                    "duration_ms": 1.0}
        raise AssertionError(argv)

    res = PackageController(WingetBackend(runner=_runner)).install(
        "Solo")
    assert res.success and res.verified
    assert res.new_version == "1.0"
    assert "verified" in res.message.lower()


def test_install_ambiguous_asks_never_guesses():
    ctl = _ctl([("list", "Python",
                 _ok("No installed package found matching input "
                     "criteria.", NOT_FOUND_CODE)),
                ("search", "Python", _ok(_PY_SEARCH))])
    res = ctl.install("Python")
    assert not res.success and res.verified is False
    assert res.error == "ambiguous_package"
    assert "Python.Python.3.12" in res.message
    assert "Python.Python.3.13" in res.message


def test_install_not_found_is_honest():
    ctl = _ctl([("list", "Nope",
                 _ok("No installed package found matching input "
                     "criteria.", NOT_FOUND_CODE)),
                ("search", "Nope",
                 _ok("No package found matching input criteria.",
                     NOT_FOUND_CODE))])
    res = ctl.install("NopeXYZ")
    assert not res.success
    assert res.error == "package_not_found"


def test_install_failure_reports_detail():
    script = [
        ("list", "Solo", _ok("No installed package found matching "
                             "input criteria.", NOT_FOUND_CODE)),
        ("search", "Solo", _ok(_table(
            [{"name": "Solo", "id": "Solo.Solo", "version": "1.0",
              "source": "winget"}]))),
        ("install", "Solo.Solo",
         {"ok": False, "exit_code": 1234, "stdout": "",
          "stderr": "Installer failed with code 1234",
          "duration_ms": 5.0}),
    ]
    res = _ctl(script).install("Solo")
    assert not res.success and not res.verified
    assert res.error == "winget_failed"


def test_install_unverified_is_not_success():
    script = [
        ("list", "Solo", _ok("No installed package found matching "
                             "input criteria.", NOT_FOUND_CODE)),
        ("search", "Solo", _ok(_table(
            [{"name": "Solo", "id": "Solo.Solo", "version": "1.0",
              "source": "winget"}]))),
        ("install", "Solo.Solo", _ok("Successfully installed")),
    ]
    # Post-install list still shows nothing → verification fails.
    res = _ctl(script).install("Solo")
    assert not res.success and not res.verified
    assert res.error == "verification_unavailable"


# ---------------------------------------------------------------------------
# UPGRADE (§9): version change, already-current, not-installed
# ---------------------------------------------------------------------------


def test_upgrade_verifies_new_version():
    state = {"version": "2.45.1"}

    def _runner(argv, timeout):
        verb = argv[1]
        if verb == "list":
            out = _installed_table("Git", "Git.Git",
                                   state["version"],
                                   "" if state["version"] != "2.45.1"
                                   else "2.55.0")
            return {"ok": True, "exit_code": 0, "stdout": out,
                    "stderr": "", "duration_ms": 1.0}
        if verb == "upgrade":
            assert argv[2:5] == ["--exact", "--id", "Git.Git"], argv
            state["version"] = "2.55.0"
            return {"ok": True, "exit_code": 0,
                    "stdout": "Successfully installed", "stderr": "",
                    "duration_ms": 1.0}
        raise AssertionError(argv)

    res = PackageController(WingetBackend(runner=_runner)).upgrade(
        "Git")
    assert res.success and res.verified
    assert res.previous_version == "2.45.1"
    assert res.new_version == "2.55.0"
    assert "2.45.1 → 2.55.0" in res.message


def test_upgrade_already_current_is_not_failure():
    backend = _FakeWinget([
        ("list", "Git", _ok(_GIT_INSTALLED)),
        ("upgrade", "Git.Git",
         {"ok": False, "exit_code": 42, "stdout": "No applicable update "
          "found.", "stderr": "", "duration_ms": 5.0}),
    ])
    res = PackageController(backend).upgrade("Git")
    assert res.success and res.verified
    assert res.evidence.get("already_current") is True
    assert "up to date" in res.message.lower()


def test_upgrade_not_installed_is_honest():
    ctl = _ctl([("list", "Nope",
                 _ok("No installed package found matching input "
                     "criteria.", NOT_FOUND_CODE))])
    res = ctl.upgrade("NopeXYZ")
    assert not res.success
    assert res.error == "not_installed"


# ---------------------------------------------------------------------------
# UNINSTALL (§10): verified absence, not-installed, failure
# ---------------------------------------------------------------------------

def test_uninstall_verifies_absence():
    state = {"installed": True}

    def _runner(argv, timeout):
        verb = argv[1]
        if verb == "list":
            if state["installed"]:
                out = _installed_table("Solo", "Solo.Solo", "1.0")
                return {"ok": True, "exit_code": 0, "stdout": out,
                        "stderr": "", "duration_ms": 1.0}
            return _not_found("No installed package found matching "
                              "input criteria.")
        if verb == "uninstall":
            assert argv[2:5] == ["--exact", "--id", "Solo.Solo"], argv
            state["installed"] = False
            return {"ok": True, "exit_code": 0,
                    "stdout": "Successfully uninstalled", "stderr": "",
                    "duration_ms": 1.0}
        raise AssertionError(argv)

    res = PackageController(WingetBackend(runner=_runner)).uninstall(
        "Solo")
    assert res.success and res.verified
    assert res.previous_version == "1.0"
    assert "no longer present" in res.message.lower()


def test_uninstall_still_present_is_not_success():
    script = [
        ("list", "Solo", _ok(_table(
            [{"name": "Solo", "id": "Solo.Solo", "version": "1.0",
              "available": "", "source": "winget"}],
            cols=("Name", "Id", "Version", "Available", "Source")))),
        ("uninstall", "Solo.Solo", _ok("Successfully uninstalled")),
    ]
    res = _ctl(script).uninstall("Solo")
    assert not res.success and not res.verified
    assert res.error == "verification_unavailable"


def test_uninstall_not_installed_is_honest():
    ctl = _ctl([("list", "Nope",
                 _ok("No installed package found matching input "
                     "criteria.", NOT_FOUND_CODE))])
    res = ctl.uninstall("NopeXYZ")
    assert not res.success
    assert res.error == "not_installed"


# ---------------------------------------------------------------------------
# BULK UPDATE (§11/§12): plan, partial failure, all-current
# ---------------------------------------------------------------------------

_UPGRADABLE = _table(
    [{"name": "A", "id": "A.A", "version": "1.0", "available": "2.0",
      "source": "winget"},
     {"name": "B", "id": "B.B", "version": "1.0", "available": "1.1",
      "source": "winget"}],
    cols=("Name", "Id", "Version", "Available", "Source"))


def test_bulk_dry_run_plans_without_acting():
    backend = _FakeWinget([("upgrade", "winget", _ok(_UPGRADABLE))])
    res = PackageController(backend).upgrade_all(dry_run=True)
    assert res.success and res.verified
    assert res.evidence["affected_count"] == 2
    assert "2 packages would update" in res.message
    assert backend.argv_log == [] or all(
        a[1] != "install" for a in backend.argv_log)


def test_bulk_partial_failure_is_not_success():
    versions = {"A.A": "1.0", "B.B": "1.0"}

    def _runner(argv, timeout):
        verb = argv[1]
        blob = " ".join(argv)
        if verb == "upgrade" and "--id" not in argv:
            return {"ok": True, "exit_code": 0, "stdout": _UPGRADABLE,
                    "stderr": "", "duration_ms": 1.0}
        if verb == "list":
            pid = argv[argv.index("--id") + 1] \
                if "--id" in argv else ""
            out = _installed_table(pid.split(".")[0], pid,
                                   versions.get(pid, "?"),
                                   "" if pid == "A.A"
                                   and versions.get(pid) != "1.0"
                                   else "9.9")
            return {"ok": True, "exit_code": 0, "stdout": out,
                    "stderr": "", "duration_ms": 1.0}
        if verb == "upgrade" and "A.A" in blob:
            versions["A.A"] = "2.0"
            return {"ok": True, "exit_code": 0, "stdout": "ok",
                    "stderr": "", "duration_ms": 1.0}
        if verb == "upgrade" and "B.B" in blob:
            return {"ok": False, "exit_code": 99, "stdout": "",
                    "stderr": "boom", "duration_ms": 1.0}
        raise AssertionError(argv)

    res = PackageController(WingetBackend(runner=_runner)).upgrade_all()
    assert not res.success and res.verified
    assert res.error == "partial_failure"
    assert res.evidence["successful"] == 1
    assert res.evidence["failed"] == 1
    assert "1 updated" in res.message and "1 failed" in res.message


def test_bulk_nothing_to_do_is_honest_success():
    backend = _FakeWinget([("upgrade", "winget",
                            _ok("No installed package found matching "
                                "input criteria.", NOT_FOUND_CODE))])
    res = PackageController(backend).upgrade_all()
    assert res.success and res.verified
    assert "up to date" in res.message.lower()


# ---------------------------------------------------------------------------
# Permissions (§6): existing tiers only, destructive confirms
# ---------------------------------------------------------------------------

def test_package_permission_tiers():
    engine = PermissionEngine()
    assert engine.evaluate(
        "package_search", {"package": "Git"})[0] == \
        PermissionLevel.HARMLESS
    assert engine.evaluate(
        "package_inspect", {"package": "Git"})[0] == \
        PermissionLevel.HARMLESS
    assert engine.evaluate(
        "package_install", {"package": "Git"})[0] == \
        PermissionLevel.SENSITIVE
    assert engine.evaluate(
        "package_upgrade", {"package": "Git"})[0] == \
        PermissionLevel.SENSITIVE
    assert engine.evaluate(
        "package_upgrade_all", {})[0] == PermissionLevel.SENSITIVE
    level, confirm = engine.evaluate(
        "package_uninstall", {"package": "Git"})
    assert level == PermissionLevel.DESTRUCTIVE
    assert confirm is True  # destructive always confirms
    assert {level.value for level in PermissionLevel} == {
        "harmless", "normal", "sensitive", "destructive"}


def test_harmless_reads_need_no_confirmation():
    engine = PermissionEngine()
    for tool in ("package_search", "package_inspect"):
        assert engine.evaluate(tool, {"package": "Git"})[1] is False


# ---------------------------------------------------------------------------
# Planner contract: schemas accept pilot tools, reject bad payloads
# ---------------------------------------------------------------------------

_PLANNER_ACCEPT = [
    {"goal": "g", "steps": [{"tool": "package_search",
                             "package": "Git"}]},
    {"goal": "g", "steps": [{"tool": "package_inspect",
                             "package": "Git"}]},
    {"goal": "g", "steps": [{"tool": "package_inspect"}]},
    {"goal": "g", "steps": [{"tool": "package_install",
                             "package": "Git"}]},
    {"goal": "g", "steps": [{"tool": "package_install",
                             "package": "Git",
                             "package_id": "Git.Git"}]},
    {"goal": "g", "steps": [{"tool": "package_upgrade",
                             "package": "Git"}]},
    {"goal": "g", "steps": [{"tool": "package_uninstall",
                             "package": "Git"}]},
    {"goal": "g", "steps": [{"tool": "package_upgrade_all"}]},
    {"goal": "g", "steps": [{"tool": "package_upgrade_all",
                             "dry_run": True,
                             "max_packages": 5}]},
]

_PLANNER_REJECT = [
    {"goal": "g", "steps": [{"tool": "package_search"}]},
    {"goal": "g", "steps": [{"tool": "package_install"}]},
    {"goal": "g", "steps": [{"tool": "package_install",
                             "package": ""}]},
    {"goal": "g", "steps": [{"tool": "package_upgrade",
                             "package": "Git",
                             "timeout": -5}]},
    {"goal": "g", "steps": [{"tool": "package_upgrade_all",
                             "max_packages": 99}]},
    {"goal": "g", "steps": [{"tool": "package_upgrade_all",
                             "skip": "Git"}]},
    {"goal": "g", "steps": [{"tool": "package_inspect",
                             "package": 42}]},
]


@pytest.mark.parametrize("plan", _PLANNER_ACCEPT)
def test_planner_accepts_package_tools(plan):
    TaskPlanner._validate_plan(plan)


@pytest.mark.parametrize("plan", _PLANNER_REJECT)
def test_planner_rejects_bad_package_payloads(plan):
    with pytest.raises(PlannerValidationError):
        TaskPlanner._validate_plan(plan)


# ---------------------------------------------------------------------------
# Executor integration + result schema (§3)
# ---------------------------------------------------------------------------

def test_executor_package_search_end_to_end(monkeypatch):
    from tools import executor as executor_mod
    ctl = _ctl([("search", "Git", _ok(_GIT_SEARCH))])
    monkeypatch.setattr(executor_mod.TaskExecutor, "__init__",
                        lambda self: setattr(self, "packages", ctl))
    ex = executor_mod.TaskExecutor()
    res = ex._execute_step("package_search",
                           {"tool": "package_search",
                            "package": "Git"})
    assert res.success and res.data["verified"] is True
    assert res.data["operation"] == "search"


def test_executor_rejects_bad_package_steps():
    ex = TaskExecutor.__new__(TaskExecutor)
    assert ex._validate_step(
        "package_install",
        {"tool": "package_install", "package": ""}) is not None
    assert ex._validate_step(
        "package_upgrade_all",
        {"tool": "package_upgrade_all",
         "max_packages": 99}) is not None
    assert ex._validate_step(
        "package_upgrade_all",
        {"tool": "package_upgrade_all",
         "dry_run": True}) is None
    assert ex._validate_step(
        "package_inspect",
        {"tool": "package_inspect"}) is None


def test_result_schema_keys():
    res = PackageResult(True, True, "search", "Git", None, None,
                        None, "ok", "out", "err", {}, False, None)
    assert set(res.to_dict()) == {
        "success", "verified", "operation", "package", "package_id",
        "previous_version", "new_version", "message", "stdout",
        "stderr", "evidence", "retryable", "error"}


# ---------------------------------------------------------------------------
# Registry, server phases, lessons (§12/§13/§15)
# ---------------------------------------------------------------------------

def test_registry_lists_package_tools():
    registry = build_default_tool_registry()
    assert registry.get("package_search").idempotent is True
    assert registry.get("package_inspect").idempotent is True
    assert registry.get("package_install").idempotent is False
    assert registry.get("package_upgrade").idempotent is False
    assert registry.get("package_uninstall").idempotent is False
    assert registry.get("package_upgrade_all").idempotent is False
    assert registry.validate_payload(
        "package_install", {"package": "Git"}) is None


def test_server_accepts_package_phases():
    from backend import server as server_mod
    assert server_mod._VISUAL_TOOL_PHASES["package_search"] == \
        server_mod.PHASE_PACKAGE_SEARCH
    assert server_mod._VISUAL_TOOL_PHASES["package_install"] == \
        server_mod.PHASE_PACKAGE_INSTALL
    assert server_mod._VISUAL_TOOL_PHASES["package_upgrade"] == \
        server_mod.PHASE_PACKAGE_UPDATE
    assert server_mod._VISUAL_TOOL_PHASES["package_upgrade_all"] == \
        server_mod.PHASE_PACKAGE_UPDATE
    assert server_mod._VISUAL_TOOL_PHASES["package_uninstall"] == \
        server_mod.PHASE_PACKAGE_UNINSTALL
    for phase in (server_mod.PHASE_PACKAGE_SEARCH,
                  server_mod.PHASE_PACKAGE_INSPECT,
                  server_mod.PHASE_PACKAGE_INSTALL,
                  server_mod.PHASE_PACKAGE_UPDATE,
                  server_mod.PHASE_PACKAGE_UNINSTALL):
        assert phase in server_mod.VALID_PHASES
    assert server_mod._VISUAL_STAGE_LABELS["package_install"] == \
        "PACKAGE INSTALL"


def test_lessons_record_resolution_and_outcome():
    ctl = _ctl([("list", "Git", _ok(_GIT_INSTALLED))])
    lesson = PackageController.build_lesson(ctl.inspect("Git"))
    assert lesson["outcome"] == "success"
    assert "Git.Git" in lesson["lesson"]
    assert "screenshot" not in str(lesson).lower()


def test_lessons_record_ambiguity_lesson():
    ctl = _ctl([("list", "Python",
                 _ok("No installed package found matching input "
                     "criteria.", NOT_FOUND_CODE)),
                ("search", "Python", _ok(_PY_SEARCH))])
    lesson = PackageController.build_lesson(ctl.install("Python"))
    assert lesson["outcome"] == "failure"
    assert "ask" in lesson["lesson"]
