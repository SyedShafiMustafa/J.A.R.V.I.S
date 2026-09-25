"""
tools/packages.py
-----------------
§30 — Windows package management through winget.

Reusable package abstraction over the existing JARVIS stack. No parallel
framework: the planner, router, TaskExecutor, LiveToolRunner,
PermissionEngine, experience memory and HUD stay exactly as they are —
this module only adds *package backends* behind six tools
(``package_search`` / ``package_inspect`` / ``package_install`` /
``package_upgrade`` / ``package_uninstall`` / ``package_upgrade_all``).

Control strategy per operation (structured argv, honest last):

    1. RESOLVE the package to an exact winget id (never the first
       arbitrary match — ambiguity returns candidates and stops).
    2. ACT through winget with structured arguments (no shell strings),
       bounded timeouts and captured stdout/stderr/exit code.
    3. VERIFY by querying installed state again (presence/version for
       install/upgrade, absence for uninstall).
    4. Otherwise report an honest failure — never a bare claim.

Every operation returns a ``PackageResult`` with per-stage latencies so
experience memory and the HUD can tell the paths apart.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_log = logging.getLogger("jarvis.packages")

# ---------------------------------------------------------------------------
# Abstraction (§3)
# ---------------------------------------------------------------------------

OPERATIONS = ("search", "inspect", "install", "upgrade", "uninstall",
              "upgrade_all")

SEARCH_TIMEOUT_S = 60.0
LIST_TIMEOUT_S = 60.0
MUTATE_TIMEOUT_S = 600.0
MIN_MUTATE_TIMEOUT_S = 60.0
MAX_MUTATE_TIMEOUT_S = 1800.0
MAX_BULK_PACKAGES = 20
MAX_SEARCH_RESULTS = 10
MAX_LIST_RESULTS = 30


@dataclass
class PackageRequest:
    operation: str
    package: Optional[str] = None
    package_id: Optional[str] = None
    version: Optional[str] = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class PackageResult:
    success: bool
    verified: bool
    operation: str = ""
    package: Optional[str] = None
    package_id: Optional[str] = None
    previous_version: Optional[str] = None
    new_version: Optional[str] = None
    message: str = ""
    stdout: str = ""
    stderr: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    retryable: bool = True
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "verified": self.verified,
            "operation": self.operation,
            "package": self.package,
            "package_id": self.package_id,
            "previous_version": self.previous_version,
            "new_version": self.new_version,
            "message": self.message,
            "stdout": (self.stdout or "")[:2000],
            "stderr": (self.stderr or "")[:1000],
            "evidence": dict(self.evidence),
            "retryable": self.retryable,
            "error": self.error,
        }


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


# ---------------------------------------------------------------------------
# Known package hints (§5). Hints only — resolution always verifies through
# winget search/list, and ambiguity never installs a guess.
# ---------------------------------------------------------------------------

KNOWN_PACKAGES = {
    "git": "Git.Git",
    "vs code": "Microsoft.VisualStudioCode",
    "vscode": "Microsoft.VisualStudioCode",
    "notepad++": "Notepad++.Notepad++",
    "7zip": "7zip.7zip",
    "7-zip": "7zip.7zip",
    "vlc": "VideoLAN.VLC",
    "chrome": "Google.Chrome",
    "firefox": "Mozilla.Firefox",
    "nodejs": "OpenJS.NodeJS",
    "node": "OpenJS.NodeJS",
    "windows terminal": "Microsoft.WindowsTerminal",
    "terminal": "Microsoft.WindowsTerminal",
    "powertoys": "Microsoft.PowerToys",
    "everything": "voidtools.Everything",
}

NOT_FOUND_CODE = -1978335212  # observed: no package / no installed package


# ---------------------------------------------------------------------------
# winget table parsing
# ---------------------------------------------------------------------------

def _parse_table(text: str) -> list[dict[str, str]]:
    """Parse a winget table via header-label column positions.

    winget pads every cell to its column width and separates the
    header with one solid dash run, so column *starts* come from the
    header labels (Name/Id/Version/Available/Match/Source) and each
    column runs until the next one starts. Tolerant of missing
    columns: only labels present in the header are extracted.
    """
    lines = [line.rstrip("\n") for line in (text or "").splitlines()]
    header_idx = None
    for i, line in enumerate(lines):
        low = line.lower()
        if "name" in low and ("id" in low or "identifier" in low):
            header_idx = i
            break
    if header_idx is None or header_idx + 1 >= len(lines):
        return []
    header = lines[header_idx]
    dashes = lines[header_idx + 1]
    if not re.fullmatch(r"[ \-\x2014]+", dashes) or "-" not in dashes:
        return []
    labels = ["name", "identifier", "id", "version", "available",
              "match", "source"]
    found: list[tuple[int, str]] = []
    for label in labels:
        match = re.search(r"\b" + label + r"\b", header.lower())
        if match:
            name = "id" if label == "identifier" else label
            found.append((match.start(), name))
    found.sort()
    # Drop a shadowed "id" when "identifier" also matched nearby — keep
    # the first occurrence per name.
    cols: list[tuple[str, int]] = []
    seen: set[str] = set()
    for pos, name in found:
        if name in seen:
            continue
        seen.add(name)
        cols.append((name, pos))
    if not cols or cols[0][0] != "name":
        return []
    rows: list[dict[str, str]] = []
    for line in lines[header_idx + 2:]:
        if not line.strip():
            continue
        row: dict[str, str] = {}
        for idx, (label, start) in enumerate(cols):
            end = cols[idx + 1][1] if idx + 1 < len(cols) else None
            cell = line[start:end] if start < len(line) else ""
            row[label] = cell.strip()
        if not row.get("name") and not row.get("id"):
            continue
        rows.append(row)
    return rows


def _is_not_found(exit_code: int, output: str) -> bool:
    if exit_code == NOT_FOUND_CODE:
        return True
    return bool(re.search(r"no (installed )?package found",
                          output or "", re.IGNORECASE))


def _classify_failure(exit_code: int, output: str) -> tuple[str, str, bool]:
    """Map winget failure to (error, detail, retryable)."""
    blob = f"{output or ''}"
    if _is_not_found(exit_code, blob):
        return ("package_not_found",
                "no package matched that name", False)
    if re.search(r"multiple.*match|more than one|ambiguous", blob,
                 re.IGNORECASE):
        return ("ambiguous_package",
                "several packages matched; be more specific", False)
    if re.search(r"administrat|elevation|UAC|0x80073[dD]", blob):
        return ("elevation_required",
                "that installer needs administrator approval", False)
    if re.search(r"network|0x8A1500(04|05)|source.*fail|failed to retrieve|"
                 r"internet|DNS|0x80190193", blob, re.IGNORECASE):
        return ("network_failure",
                "winget couldn't reach its source", True)
    if re.search(r"already installed|already.*latest|no applicable update|"
                 r"is already up to date|newer.*already", blob,
                 re.IGNORECASE):
        return ("already_current",
                "already at the requested state", False)
    return ("winget_failed",
            f"winget exited with code {exit_code}", True)


# ---------------------------------------------------------------------------
# winget backend (§4): structured argv, captured results, bounded time
# ---------------------------------------------------------------------------

class WingetBackend:
    """Run winget with argument lists (never shell strings)."""

    def __init__(self, runner: Optional[Callable[..., dict]] = None):
        self._runner = runner or self._run

    def available(self) -> bool:
        return shutil.which("winget") is not None

    @staticmethod
    def _run(argv: list[str], timeout: float) -> dict[str, Any]:
        start = _now_ms()
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True,
                # winget emits bytes outside the console code page
                # (CJK names, progress glyphs) — never let decoding
                # kill the reader thread and fake an empty result.
                encoding="utf-8", errors="replace",
                timeout=timeout, shell=False)
            return {"ok": proc.returncode == 0,
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout or "",
                    "stderr": proc.stderr or "",
                    "duration_ms": _now_ms() - start}
        except subprocess.TimeoutExpired as exc:
            out = ""
            err = ""
            try:
                out = (exc.stdout or b"").decode("utf-8", "replace") \
                    if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                err = (exc.stderr or b"").decode("utf-8", "replace") \
                    if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            except Exception:
                pass
            return {"ok": False, "exit_code": None, "stdout": out,
                    "stderr": err, "duration_ms": _now_ms() - start,
                    "reason": "timeout"}
        except FileNotFoundError:
            return {"ok": False, "exit_code": None, "stdout": "",
                    "stderr": "", "duration_ms": _now_ms() - start,
                    "reason": "winget_unavailable"}
        except Exception as exc:
            return {"ok": False, "exit_code": None, "stdout": "",
                    "stderr": "", "duration_ms": _now_ms() - start,
                    "reason": "launch_failed",
                    "error": str(exc)[:200]}

    # -- read-only -------------------------------------------------------
    def search(self, query: str,
               timeout: float = SEARCH_TIMEOUT_S) -> dict[str, Any]:
        res = self._runner(
            ["winget", "search", query, "--disable-interactivity",
             "--accept-source-agreements"], timeout)
        if res.get("reason") in ("winget_unavailable", "launch_failed"):
            return {"ok": False, **res}
        if res.get("reason") == "timeout":
            return {"ok": False, "reason": "timeout", **res}
        blob = f"{res.get('stdout', '')}\n{res.get('stderr', '')}"
        if not res.get("ok") and _is_not_found(res.get("exit_code", 0),
                                               blob):
            res["results"] = []
            return res
        res["results"] = _parse_table(res.get("stdout", ""))
        return res

    def list_installed(self, name: Optional[str] = None,
                       package_id: Optional[str] = None,
                       timeout: float = LIST_TIMEOUT_S) -> dict[str, Any]:
        argv = ["winget", "list", "--disable-interactivity"]
        if package_id:
            argv += ["--id", package_id, "--exact"]
        elif name:
            argv += ["--name", name]
        res = self._runner(argv, timeout)
        if res.get("reason") in ("winget_unavailable", "launch_failed",
                                 "timeout"):
            return {"ok": False, **res}
        blob = f"{res.get('stdout', '')}\n{res.get('stderr', '')}"
        if not res.get("ok") and _is_not_found(res.get("exit_code", 0),
                                               blob):
            res["results"] = []
            return res
        res["results"] = _parse_table(res.get("stdout", ""))
        return res

    def list_upgradable(self, timeout: float = LIST_TIMEOUT_S) -> dict:
        res = self._runner(["winget", "upgrade",
                            "--disable-interactivity"], timeout)
        if res.get("reason") in ("winget_unavailable", "launch_failed",
                                 "timeout"):
            return {"ok": False, **res}
        blob = f"{res.get('stdout', '')}\n{res.get('stderr', '')}"
        if not res.get("ok") and _is_not_found(res.get("exit_code", 0),
                                               blob):
            res["results"] = []
            res["ok"] = True
            return res
        res["results"] = _parse_table(res.get("stdout", ""))
        return res

    # -- mutating ----------------------------------------------------------
    def install(self, package_id: str,
                version: Optional[str] = None,
                timeout: float = MUTATE_TIMEOUT_S) -> dict[str, Any]:
        argv = ["winget", "install", "--exact", "--id", package_id,
                "--silent", "--disable-interactivity",
                "--accept-package-agreements",
                "--accept-source-agreements"]
        if version:
            argv += ["--version", version]
        return self._runner(argv, timeout)

    def upgrade(self, package_id: str,
                timeout: float = MUTATE_TIMEOUT_S) -> dict[str, Any]:
        return self._runner(
            ["winget", "upgrade", "--exact", "--id", package_id,
             "--silent", "--disable-interactivity",
             "--accept-package-agreements",
             "--accept-source-agreements"], timeout)

    def uninstall(self, package_id: str,
                  timeout: float = MUTATE_TIMEOUT_S) -> dict[str, Any]:
        return self._runner(
            ["winget", "uninstall", "--exact", "--id", package_id,
             "--disable-interactivity"], timeout)


# ---------------------------------------------------------------------------
# Resolution (§5): exact id → exact name → known hint → unique → ambiguous
# ---------------------------------------------------------------------------

def resolve_remote(query: str, results: list[dict[str, str]],
                   known_id: Optional[str] = None) -> dict[str, Any]:
    """Pick one remote candidate or return ambiguity with candidates."""
    if not results:
        return {"ok": False, "reason": "package_not_found"}
    low = (query or "").strip().lower()
    for row in results:
        if row.get("id", "").strip().lower() == low and low:
            return {"ok": True, "package_id": row["id"].strip(),
                    "row": row, "strategy": "exact_id"}
    exact_names = [row for row in results
                   if row.get("name", "").strip().lower() == low]
    if len(exact_names) == 1:
        return {"ok": True, "package_id": exact_names[0]["id"].strip(),
                "row": exact_names[0], "strategy": "exact_name"}
    if known_id:
        hit = [row for row in results
               if row.get("id", "").strip() == known_id]
        if hit:
            if len(exact_names) > 1:
                cands = [{"name": r.get("name"), "id": r.get("id")}
                         for r in exact_names[:8]]
                return {"ok": False, "reason": "ambiguous_package",
                        "candidates": cands}
            return {"ok": True, "package_id": known_id,
                    "row": hit[0], "strategy": "known_id"}
    if len(results) == 1:
        row = results[0]
        return {"ok": True, "package_id": row["id"].strip(),
                "row": row, "strategy": "unique_match"}
    cands = [{"name": r.get("name"), "id": r.get("id"),
              "version": r.get("version")}
             for r in results[:8]]
    return {"ok": False, "reason": "ambiguous_package",
            "candidates": cands}


def resolve_installed(query: str, package_id: Optional[str],
                      rows: list[dict[str, str]]) -> dict[str, Any]:
    """Match installed rows: exact id first, then unique name."""
    if not rows:
        return {"ok": False, "reason": "not_installed"}
    if package_id:
        hit = [r for r in rows
               if r.get("id", "").strip() == package_id]
        if hit:
            return {"ok": True, "row": hit[0], "strategy": "exact_id"}
        return {"ok": False, "reason": "not_installed"}
    low = (query or "").strip().lower()
    for row in rows:
        if row.get("id", "").strip().lower() == low and low:
            return {"ok": True, "row": row, "strategy": "exact_id"}
    exact = [r for r in rows
             if r.get("name", "").strip().lower() == low]
    if len(exact) == 1:
        return {"ok": True, "row": exact[0], "strategy": "exact_name"}
    if len(rows) == 1:
        return {"ok": True, "row": rows[0], "strategy": "unique_match"}
    cands = [{"name": r.get("name"), "id": r.get("id"),
              "version": r.get("version")} for r in rows[:8]]
    return {"ok": False, "reason": "ambiguous_package",
            "candidates": cands}


# ---------------------------------------------------------------------------
# Natural-language parsing (§7)
# ---------------------------------------------------------------------------
#
# Returns (tool, payload) for explicit package requests, else None.
# Explicit how-to questions return None so the brain explains — checked
# FIRST. Browser/file/WhatsApp searches are excluded from package search.

_NON_PACKAGE_SEARCH = (
    "google", "youtube", "whatsapp", "file", "files", "folder",
    "folders", "directory", "web", "image", "images", "map", "maps",
    "mail", "email", "contact", "chat",
)

_INSTALL = (
    r"\binstall\b.{0,40}",
    r"\bset\s+up\b.{0,40}",
)
_UPGRADE = (
    r"\bupdat(e|es|ing)\b.{0,40}",
    r"\bupgrad(e|es|ing)\b.{0,40}",
)
_UNINSTALL = (
    r"\buninstall\b.{0,40}",
    r"\bremov(e|es|ing)\b.{0,25}\b(app|program|package|software)\b",
)
_INSPECT = (
    r"\bis\b.{0,40}\binstall(ed)?\b",
    r"\b(install(ed)?\s+)?version\b.{0,25}\bof\b",
    r"\bwhat\s+version\b",
    r"\bshow\b.{0,30}\b(install(ed)?|version)\b",
    r"\blist\b.{0,30}\binstall(ed)?\b",
    r"\bcheck\b.{0,20}\b(install|version)\b",
)
_SEARCH = (
    r"\bsearch\b.{0,15}\bfor\b",
    r"\bfind\b.{0,15}\bpackage\b",
    r"\blook\s+(for|up)\b",
)
_UPGRADE_ALL = (
    r"\bupdat(e|es|ing)\b.{0,20}\ball\b",
    r"\bupgrad(e|es|ing)\b.{0,20}\ball\b",
    r"\bupdate\b.{0,25}\b(packages|everything)\b",
    r"\bupgrade\b.{0,25}\b(packages|everything)\b",
)


def _strip_package_noise(text: str) -> str:
    low = (text or "").lower()
    low = re.sub(r"\b(winget\s+)?packag(es?)?\b", " ", low)
    low = re.sub(r"\b(app|application|program|software|tool)\b", " ", low)
    low = re.sub(r"\b(on|for|from)\s+(my\s+)?(pc|computer|windows|system)\b",
                 " ", low)
    low = re.sub(r"\b(my|the|a|an|latest|newest|me|installed|install)\b",
                 " ", low)
    low = re.sub(r"\s+", " ", low).strip(" ?.!,")
    return low


def _extract_name(text: str, verbs: tuple[str, ...]) -> Optional[str]:
    low = (text or "").lower()
    for verb in verbs:
        match = re.search(r"\b" + verb + r"\b", low)
        if match:
            rest = low[match.end():]
            # Drop trailing purpose clauses ("for me", "to ...").
            rest = re.split(r"\bfor\s+me\b|\bplease\b", rest)[0]
            name = _strip_package_noise(rest)
            name = re.sub(r"^(all\s+|everything\s*)$", "", name).strip()
            return name or None
    return None


def parse_package_command(
        text: str) -> Optional[tuple[str, dict[str, Any]]]:
    """Map an explicit package utterance to (tool, payload)."""
    if not (text or "").strip():
        return None
    try:
        from core.intent import is_explanation_request, strip_politeness
        if is_explanation_request(text):
            return None
        core = strip_politeness(text)
    except Exception:
        core = (text or "").lower()
    low = core.lower()

    for pattern in _UPGRADE_ALL:
        if re.search(pattern, low):
            return ("package_upgrade_all", {})

    uninstall = re.search(r"\buninstall\b", low)
    if uninstall or re.search(
            r"\bremov(e|es|ing)\b.{0,25}\b(app|program|package|"
            r"software)\b", low):
        name = _extract_name(low, ("uninstall", "remove", "removes",
                                   "removing"))
        if name:
            return ("package_uninstall", {"package": name})
        return None

    if re.search(r"\binstall\b", low):
        name = _extract_name(low, ("install",))
        if name:
            return ("package_install", {"package": name})
        return None

    if re.search(r"\bupdat(e|es|ing)\b|\bupgrad(e|es|ing)\b", low):
        name = _extract_name(low, ("update", "updates", "updating",
                                   "upgrade", "upgrades", "upgrading"))
        if name:
            return ("package_upgrade", {"package": name})
        return None

    for pattern in _INSPECT:
        if re.search(pattern, low):
            if re.search(r"\b(list|show)\b.{0,30}\binstall(ed)?\b",
                         low) and not re.search(r"\b(version|of)\b",
                                                low):
                return ("package_inspect", {})
            # "What/Show ... version of X" → X.
            match = re.search(r"version\s+of\s+(.+)", low)
            if match:
                name = _strip_package_noise(
                    re.split(r"\bis\b", match.group(1))[0])
                if name:
                    return ("package_inspect", {"package": name})
            # "Is X installed?" → X.
            match = re.search(r"\bis\s+(.+?)\s+installed\b", low)
            if match:
                name = _strip_package_noise(match.group(1))
                if name:
                    return ("package_inspect", {"package": name})
            name = _extract_name(
                low, ("show", "check", "version", "of", "for", "is"))
            if name:
                return ("package_inspect", {"package": name})
            # "X is installed" with the subject first.
            subj = re.split(r"\bis\b", low)[0]
            subj = _strip_package_noise(subj)
            if subj:
                return ("package_inspect", {"package": subj})
            return None

    for pattern in _SEARCH:
        if re.search(pattern, low):
            if any(word in low for word in _NON_PACKAGE_SEARCH):
                return None
            name = _extract_name(low, ("search", "find", "look"))
            name = (name or "").replace("for ", " ").strip()
            if name:
                return ("package_search", {"package": name})
            return None

    return None


# ---------------------------------------------------------------------------
# Controller: RESOLVE → ACT → VERIFY (§8/§9/§10/§11/§12/§14)
# ---------------------------------------------------------------------------

class PackageController:
    """Owns the winget backend; UI surface stays in executor/server."""

    def __init__(self, backend: Optional[WingetBackend] = None):
        self.backend = backend or WingetBackend()

    # -- guards ----------------------------------------------------------
    def _guard(self, operation: str) -> Optional[PackageResult]:
        if not self.backend.available():
            return PackageResult(
                False, False, operation, None, None, None, None,
                "Winget isn't available on this machine, so I can't "
                "manage packages.", "", "",
                {"operation": operation}, retryable=False,
                error="winget_unavailable")
        return None

    @staticmethod
    def _timeout(value: Any) -> float:
        try:
            secs = float(value)
        except (TypeError, ValueError):
            return MUTATE_TIMEOUT_S
        return max(MIN_MUTATE_TIMEOUT_S,
                   min(MAX_MUTATE_TIMEOUT_S, secs))

    # -- SEARCH (§1) -------------------------------------------------------
    def search(self, package: str) -> PackageResult:
        start = _now_ms()
        blocked = self._guard("search")
        if blocked:
            return blocked
        name = (package or "").strip()
        if not name:
            return PackageResult(
                False, False, "search", package, None, None, None,
                "Tell me which package to search for.",
                "", "", {"operation": "search"}, retryable=False,
                error="invalid_package")
        res = self.backend.search(name)
        evidence = {"operation": "search", "query": name,
                    "duration_ms": res.get("duration_ms", 0.0),
                    "total_ms": _now_ms() - start}
        if res.get("reason") == "timeout":
            return PackageResult(
                False, False, "search", name, None, None, None,
                f"Searching for '{name}' timed out.", "", "",
                evidence, error="timeout")
        if res.get("reason") in ("winget_unavailable", "launch_failed"):
            return PackageResult(
                False, False, "search", name, None, None, None,
                "Winget isn't available on this machine.", "", "",
                evidence, retryable=False,
                error="winget_unavailable")
        rows = res.get("results", [])[:MAX_SEARCH_RESULTS]
        total = len(res.get("results", []))
        evidence["count"] = total
        if not rows:
            return PackageResult(
                True, True, "search", name, None, None, None,
                f"I couldn't find any package matching '{name}'.",
                res.get("stdout", ""), res.get("stderr", ""),
                evidence, retryable=False)
        shown = ", ".join(
            f"{r.get('name')} ({r.get('id')})" for r in rows[:5])
        more = f" (+{total - 5} more)" if total > 5 else ""
        return PackageResult(
            True, True, "search", name, None, None, None,
            f"Found {total} match{'es' if total != 1 else ''} "
            f"for '{name}': {shown}{more}.",
            res.get("stdout", ""), res.get("stderr", ""),
            {**evidence, "results": [
                {"name": r.get("name"), "id": r.get("id"),
                 "version": r.get("version")} for r in rows]},
            retryable=False)

    # -- INSPECT (§1) ------------------------------------------------------
    def inspect(self, package: Optional[str] = None,
                package_id: Optional[str] = None) -> PackageResult:
        start = _now_ms()
        blocked = self._guard("inspect")
        if blocked:
            return blocked
        if not (package or "").strip() and not package_id:
            res = self.backend.list_installed()
            evidence = {"operation": "inspect",
                        "duration_ms": res.get("duration_ms", 0.0),
                        "total_ms": _now_ms() - start}
            if res.get("reason") == "timeout":
                return PackageResult(
                    False, False, "inspect", None, None, None, None,
                    "Listing installed packages timed out.", "", "",
                    evidence, error="timeout")
            rows = res.get("results", [])
            evidence["count"] = len(rows)
            shown = ", ".join(
                f"{r.get('name')} {r.get('version', '')}".strip()
                for r in rows[:MAX_LIST_RESULTS])
            suffix = f" (+{len(rows) - MAX_LIST_RESULTS} more)" \
                if len(rows) > MAX_LIST_RESULTS else ""
            return PackageResult(
                True, True, "inspect", None, None, None, None,
                f"{len(rows)} packages installed: {shown}{suffix}.",
                res.get("stdout", ""), res.get("stderr", ""),
                evidence, retryable=False)
        res = self.backend.list_installed(name=(package or "").strip()
                                          or None,
                                          package_id=package_id)
        evidence = {"operation": "inspect",
                    "package": package, "package_id": package_id,
                    "duration_ms": res.get("duration_ms", 0.0),
                    "total_ms": _now_ms() - start}
        if res.get("reason") == "timeout":
            return PackageResult(
                False, False, "inspect", package, package_id, None,
                None, f"Checking '{package or package_id}' timed out.",
                "", "", evidence, error="timeout")
        resolved = resolve_installed((package or ""), package_id,
                                     res.get("results", []))
        if resolved.get("reason") == "ambiguous_package":
            cands = ", ".join(
                f"{c['name']} ({c['id']})"
                for c in resolved["candidates"][:5])
            return PackageResult(
                False, False, "inspect", package, package_id, None,
                None,
                f"Several installed packages match '{package}': "
                f"{cands}. Which one do you mean?",
                res.get("stdout", ""), res.get("stderr", ""),
                {**evidence,
                 "candidates": resolved["candidates"]},
                retryable=False, error="ambiguous_package")
        if not resolved.get("ok"):
            return PackageResult(
                True, True, "inspect", package, package_id, None,
                None, f"'{package or package_id}' is not installed.",
                res.get("stdout", ""), res.get("stderr", ""),
                evidence, retryable=False)
        row = resolved["row"]
        version = row.get("version", "")
        available = row.get("available", "")
        pid = row.get("id", "")
        msg = f"'{row.get('name')}' is installed (version {version})."
        if available:
            msg += f" Version {available} is available."
        return PackageResult(
            True, True, "inspect", package, pid, version, version,
            msg, res.get("stdout", ""), res.get("stderr", ""),
            {**evidence, "available": available or None},
            retryable=False)

    # -- INSTALL (§8) ------------------------------------------------------
    def install(self, package: str,
                package_id: Optional[str] = None,
                version: Optional[str] = None,
                timeout: Any = MUTATE_TIMEOUT_S) -> PackageResult:
        start = _now_ms()
        blocked = self._guard("install")
        if blocked:
            return blocked
        name = (package or "").strip()
        if not name and not package_id:
            return PackageResult(
                False, False, "install", package, package_id, None,
                None, "Tell me which package to install.", "", "",
                {"operation": "install"}, retryable=False,
                error="invalid_package")
        secs = self._timeout(timeout)

        # 1. Already installed? The installed list is ground truth —
        # report it without touching the network or guessing.
        current = self.backend.list_installed(
            name=name or None, package_id=package_id)
        hit = resolve_installed(name, package_id,
                                current.get("results", []))
        if hit.get("ok"):
            row = hit["row"]
            ver = row.get("version", "")
            pid = row.get("id", "")
            return PackageResult(
                True, True, "install", name, pid or package_id,
                ver, ver,
                f"'{row.get('name')}' is already installed "
                f"(version {ver}) — nothing to do.",
                current.get("stdout", ""), "",
                {"operation": "install",
                 "strategy": hit.get("strategy", "installed"),
                 "already_installed": True,
                 "total_ms": _now_ms() - start},
                retryable=False)

        # 2. RESOLVE remotely (never the first arbitrary match).
        found = self.backend.search(package_id or name)
        if found.get("reason") in ("timeout",):
            return PackageResult(
                False, False, "install", name, package_id, None,
                None, f"Resolving '{name}' timed out.", "", "",
                {"operation": "install",
                 "total_ms": _now_ms() - start}, error="timeout")
        known = KNOWN_PACKAGES.get((name or "").lower())
        if package_id:
            pid: Optional[str] = package_id
            strategy = "explicit_id"
        else:
            remote = resolve_remote(name, found.get("results", []),
                                    known)
            if not remote.get("ok"):
                if remote.get("reason") == "ambiguous_package":
                    cands = ", ".join(
                        f"{c['name']} ({c['id']})"
                        for c in remote["candidates"][:5])
                    return PackageResult(
                        False, False, "install", name, None, None,
                        None,
                        f"Several packages match '{name}': {cands}. "
                        f"Which one should I install?",
                        found.get("stdout", ""),
                        found.get("stderr", ""),
                        {"operation": "install",
                         "candidates": remote["candidates"],
                         "total_ms": _now_ms() - start},
                        retryable=False, error="ambiguous_package")
                return PackageResult(
                    False, True, "install", name, None, None, None,
                    f"I couldn't find any package matching '{name}'.",
                    found.get("stdout", ""),
                    found.get("stderr", ""),
                    {"operation": "install",
                     "total_ms": _now_ms() - start},
                    retryable=False, error="package_not_found")
            pid = remote["package_id"]
            strategy = remote["strategy"]

        # 3. Raced install? Re-check presence before acting.
        current = self.backend.list_installed(package_id=pid)
        hit = resolve_installed(name, pid, current.get("results", []))
        if hit.get("ok"):
            row = hit["row"]
            ver = row.get("version", "")
            return PackageResult(
                True, True, "install", name, pid, ver, ver,
                f"'{row.get('name')}' is already installed "
                f"(version {ver}) — nothing to do.",
                current.get("stdout", ""), "",
                {"operation": "install", "strategy": strategy,
                 "already_installed": True,
                 "total_ms": _now_ms() - start},
                retryable=False)

        # 4. ACT, then VERIFY presence.
        t = _now_ms()
        done = self.backend.install(pid, version, secs)
        evidence = {"operation": "install", "strategy": strategy,
                    "action_ms": _now_ms() - t,
                    "exit_code": done.get("exit_code"),
                    "duration_ms": done.get("duration_ms", 0.0)}
        if done.get("reason") == "timeout":
            return PackageResult(
                False, False, "install", name, pid, None, None,
                f"Installing '{name}' timed out after {secs:g}s.",
                done.get("stdout", ""), done.get("stderr", ""),
                {**evidence, "total_ms": _now_ms() - start},
                error="timeout")
        if not done.get("ok"):
            blob = f"{done.get('stdout', '')}\n{done.get('stderr', '')}"
            error, detail, retryable = _classify_failure(
                done.get("exit_code", -1), blob)
            if error == "already_current":
                return PackageResult(
                    True, True, "install", name, pid, None, None,
                    f"'{name}' is already installed — nothing to do.",
                    done.get("stdout", ""), done.get("stderr", ""),
                    {**evidence, "already_installed": True,
                     "total_ms": _now_ms() - start},
                    retryable=False)
            return PackageResult(
                False, False, "install", name, pid, None, None,
                f"Installing '{name}' failed — {detail}.",
                done.get("stdout", ""), done.get("stderr", ""),
                {**evidence, "total_ms": _now_ms() - start},
                retryable=retryable, error=error)
        verify = self.backend.list_installed(package_id=pid)
        rows = verify.get("results", [])
        match = [r for r in rows if r.get("id", "").strip() == pid]
        evidence["total_ms"] = _now_ms() - start
        if not match:
            return PackageResult(
                False, False, "install", name, pid, None, None,
                f" winget finished but I can't find '{name}' "
                f"installed — not verified.".strip(),
                done.get("stdout", ""), done.get("stderr", ""),
                evidence, error="verification_unavailable")
        ver = match[0].get("version", "")
        return PackageResult(
            True, True, "install", name, pid, None, ver,
            f"'{match[0].get('name')}' installed and verified — "
            f"version {ver}.",
            done.get("stdout", ""), done.get("stderr", ""),
            evidence, retryable=False)

    # -- UPGRADE (§9) ------------------------------------------------------
    def upgrade(self, package: str,
                package_id: Optional[str] = None,
                timeout: Any = MUTATE_TIMEOUT_S) -> PackageResult:
        start = _now_ms()
        blocked = self._guard("upgrade")
        if blocked:
            return blocked
        name = (package or "").strip()
        if not name and not package_id:
            return PackageResult(
                False, False, "upgrade", package, package_id, None,
                None, "Tell me which package to update.", "", "",
                {"operation": "upgrade"}, retryable=False,
                error="invalid_package")
        secs = self._timeout(timeout)

        # 1. Must be installed first (also resolves the exact id).
        current = self.backend.list_installed(
            name=name or None, package_id=package_id)
        hit = resolve_installed(name, package_id,
                                current.get("results", []))
        if hit.get("reason") == "ambiguous_package":
            cands = ", ".join(
                f"{c['name']} ({c['id']})"
                for c in hit["candidates"][:5])
            return PackageResult(
                False, False, "upgrade", name, package_id, None,
                None,
                f"Several installed packages match '{name}': "
                f"{cands}. Which one should I update?",
                current.get("stdout", ""),
                current.get("stderr", ""),
                {"operation": "upgrade",
                 "candidates": hit["candidates"],
                 "total_ms": _now_ms() - start},
                retryable=False, error="ambiguous_package")
        if not hit.get("ok"):
            return PackageResult(
                False, True, "upgrade", name, package_id, None,
                None, f"'{name or package_id}' is not installed, "
                "so there's nothing to update.",
                current.get("stdout", ""), current.get("stderr", ""),
                {"operation": "upgrade",
                 "total_ms": _now_ms() - start},
                retryable=False, error="not_installed")
        row = hit["row"]
        pid = row.get("id", "")
        previous = row.get("version", "")

        # 2. ACT, then VERIFY the resulting version.
        t = _now_ms()
        done = self.backend.upgrade(pid, secs)
        evidence = {"operation": "upgrade",
                    "action_ms": _now_ms() - t,
                    "exit_code": done.get("exit_code"),
                    "duration_ms": done.get("duration_ms", 0.0)}
        if done.get("reason") == "timeout":
            return PackageResult(
                False, False, "upgrade", name, pid, previous, None,
                f"Updating '{name}' timed out after {secs:g}s.",
                done.get("stdout", ""), done.get("stderr", ""),
                {**evidence, "total_ms": _now_ms() - start},
                error="timeout")
        blob = f"{done.get('stdout', '')}\n{done.get('stderr', '')}"
        if not done.get("ok"):
            error, detail, retryable = _classify_failure(
                done.get("exit_code", -1), blob)
            if error == "already_current":
                return PackageResult(
                    True, True, "upgrade", name, pid, previous,
                    previous,
                    f"'{row.get('name')}' is already up to date "
                    f"(version {previous}).",
                    done.get("stdout", ""), done.get("stderr", ""),
                    {**evidence, "already_current": True,
                     "total_ms": _now_ms() - start},
                    retryable=False)
            return PackageResult(
                False, False, "upgrade", name, pid, previous, None,
                f"Updating '{name}' failed — {detail}.",
                done.get("stdout", ""), done.get("stderr", ""),
                {**evidence, "total_ms": _now_ms() - start},
                retryable=retryable, error=error)
        after = self.backend.list_installed(package_id=pid)
        rows = [r for r in after.get("results", [])
                if r.get("id", "").strip() == pid]
        evidence["total_ms"] = _now_ms() - start
        if not rows:
            return PackageResult(
                False, False, "upgrade", name, pid, previous, None,
                f" winget finished but I can't find '{name}' "
                f"anymore — not verified.".strip(),
                done.get("stdout", ""), done.get("stderr", ""),
                evidence, error="verification_unavailable")
        new = rows[0].get("version", "")
        if new and new != previous:
            return PackageResult(
                True, True, "upgrade", name, pid, previous, new,
                f"'{rows[0].get('name')}' updated and verified — "
                f"{previous} → {new}.",
                done.get("stdout", ""), done.get("stderr", ""),
                evidence, retryable=False)
        return PackageResult(
            True, True, "upgrade", name, pid, previous, new or previous,
            f"'{rows[0].get('name')}' is already up to date "
            f"(version {new or previous}).",
            done.get("stdout", ""), done.get("stderr", ""),
            {**evidence, "already_current": True},
            retryable=False)

    # -- UNINSTALL (§10) ----------------------------------------------------
    def uninstall(self, package: str,
                  package_id: Optional[str] = None,
                  timeout: Any = MUTATE_TIMEOUT_S) -> PackageResult:
        start = _now_ms()
        blocked = self._guard("uninstall")
        if blocked:
            return blocked
        name = (package or "").strip()
        if not name and not package_id:
            return PackageResult(
                False, False, "uninstall", package, package_id,
                None, None, "Tell me which package to uninstall.",
                "", "", {"operation": "uninstall"}, retryable=False,
                error="invalid_package")
        secs = self._timeout(timeout)

        # 1. Must be installed (resolves the exact id, never a guess).
        current = self.backend.list_installed(
            name=name or None, package_id=package_id)
        hit = resolve_installed(name, package_id,
                                current.get("results", []))
        if hit.get("reason") == "ambiguous_package":
            cands = ", ".join(
                f"{c['name']} ({c['id']})"
                for c in hit["candidates"][:5])
            return PackageResult(
                False, False, "uninstall", name, package_id, None,
                None,
                f"Several installed packages match '{name}': "
                f"{cands}. Which one should I remove? I won't guess.",
                current.get("stdout", ""),
                current.get("stderr", ""),
                {"operation": "uninstall",
                 "candidates": hit["candidates"],
                 "total_ms": _now_ms() - start},
                retryable=False, error="ambiguous_package")
        if not hit.get("ok"):
            return PackageResult(
                False, True, "uninstall", name, package_id, None,
                None, f"'{name or package_id}' is not installed, "
                "so there's nothing to remove.",
                current.get("stdout", ""), current.get("stderr", ""),
                {"operation": "uninstall",
                 "total_ms": _now_ms() - start},
                retryable=False, error="not_installed")
        row = hit["row"]
        pid = row.get("id", "")
        previous = row.get("version", "")

        # 2. ACT (post-confirmation), then VERIFY absence.
        t = _now_ms()
        done = self.backend.uninstall(pid, secs)
        evidence = {"operation": "uninstall",
                    "action_ms": _now_ms() - t,
                    "exit_code": done.get("exit_code"),
                    "duration_ms": done.get("duration_ms", 0.0)}
        if done.get("reason") == "timeout":
            return PackageResult(
                False, False, "uninstall", name, pid, previous,
                None,
                f"Removing '{name}' timed out after {secs:g}s — "
                f"check whether it's still installed.",
                done.get("stdout", ""), done.get("stderr", ""),
                {**evidence, "total_ms": _now_ms() - start},
                error="timeout")
        if not done.get("ok"):
            blob = f"{done.get('stdout', '')}\n{done.get('stderr', '')}"
            error, detail, retryable = _classify_failure(
                done.get("exit_code", -1), blob)
            return PackageResult(
                False, False, "uninstall", name, pid, previous,
                None, f"Removing '{name}' failed — {detail}.",
                done.get("stdout", ""), done.get("stderr", ""),
                {**evidence, "total_ms": _now_ms() - start},
                retryable=retryable, error=error)
        after = self.backend.list_installed(package_id=pid)
        rows = [r for r in after.get("results", [])
                if r.get("id", "").strip() == pid]
        evidence["total_ms"] = _now_ms() - start
        if rows:
            return PackageResult(
                False, False, "uninstall", name, pid, previous,
                rows[0].get("version"),
                f"winget finished but '{name}' is still installed "
                f"(version {rows[0].get('version', '')}) — not verified.",
                done.get("stdout", ""), done.get("stderr", ""),
                evidence, error="verification_unavailable")
        return PackageResult(
            True, True, "uninstall", name, pid, previous, None,
            f"'{row.get('name')}' uninstalled and verified — "
            f"no longer present.",
            done.get("stdout", ""), done.get("stderr", ""),
            evidence, retryable=False)

    # -- BULK UPDATE (§11/§12) -------------------------------------------------
    def upgrade_all(self, max_packages: int = MAX_BULK_PACKAGES,
                    skip: Optional[list[str]] = None,
                    dry_run: bool = False,
                    timeout: Any = MUTATE_TIMEOUT_S) -> PackageResult:
        start = _now_ms()
        blocked = self._guard("upgrade_all")
        if blocked:
            return blocked
        secs = self._timeout(timeout)
        skip_ids = {str(s).strip() for s in (skip or []) if str(s).strip()}

        discovered = self.backend.list_upgradable()
        if discovered.get("reason") == "timeout":
            return PackageResult(
                False, False, "upgrade_all", None, None, None, None,
                "Discovering upgradable packages timed out.", "", "",
                {"operation": "upgrade_all",
                 "total_ms": _now_ms() - start}, error="timeout")
        if not discovered.get("ok"):
            blob = (f"{discovered.get('stdout', '')}\n"
                    f"{discovered.get('stderr', '')}")
            error, detail, retryable = _classify_failure(
                discovered.get("exit_code", -1), blob)
            return PackageResult(
                False, False, "upgrade_all", None, None, None, None,
                f"Couldn't enumerate upgradable packages — {detail}.",
                discovered.get("stdout", ""),
                discovered.get("stderr", ""),
                {"operation": "upgrade_all",
                 "total_ms": _now_ms() - start},
                retryable=retryable, error=error)
        rows = [r for r in discovered.get("results", [])
                if r.get("id", "").strip()]
        targets = [r for r in rows
                   if r.get("id", "").strip() not in skip_ids]
        try:
            limit = max(1, min(MAX_BULK_PACKAGES, int(max_packages)))
        except (TypeError, ValueError):
            limit = MAX_BULK_PACKAGES
        planned = targets[:limit]
        skipped_overflow = targets[limit:]

        evidence: dict[str, Any] = {
            "operation": "upgrade_all",
            "affected_count": len(planned),
            "discovered_count": len(rows),
            "skipped_count": len(skip_ids) + len(skipped_overflow),
        }
        if dry_run:
            evidence["total_ms"] = _now_ms() - start
            names = ", ".join(
                f"{r.get('name')} ({r.get('id')})" for r in planned[:10])
            more = f" (+{len(planned) - 10} more)" \
                if len(planned) > 10 else ""
            return PackageResult(
                True, True, "upgrade_all", None, None, None, None,
                f"Upgrade plan: {len(planned)} package"
                f"{'s' if len(planned) != 1 else ''} would update "
                f"({names}{more}). Nothing changed.",
                discovered.get("stdout", ""), "",
                {**evidence, "dry_run": True}, retryable=False)

        per_package: list[dict[str, Any]] = []
        successful = already = failed = 0
        for row in planned:
            pid = row.get("id", "").strip()
            one = self.upgrade(row.get("name", "") or pid,
                               package_id=pid, timeout=secs)
            status = "successful" if one.success else "failed"
            if one.success and one.evidence.get("already_current"):
                status = "already_current"
                already += 1
            elif one.success:
                successful += 1
            else:
                failed += 1
            per_package.append({
                "name": row.get("name"), "id": pid,
                "from": one.previous_version, "to": one.new_version,
                "status": status, "error": one.error})
        for row in skipped_overflow:
            per_package.append({
                "name": row.get("name"), "id": row.get("id"),
                "status": "skipped", "error": "over_bulk_limit"})
        evidence.update({
            "successful": successful, "already_current": already,
            "failed": failed,
            "skipped": len(skip_ids) + len(skipped_overflow),
            "per_package": per_package,
            "total_ms": _now_ms() - start,
        })
        if not planned:
            return PackageResult(
                True, True, "upgrade_all", None, None, None, None,
                "Everything is already up to date — nothing to do.",
                discovered.get("stdout", ""), "",
                evidence, retryable=False)
        summary = (f"Bulk update: {successful} updated, {already} "
                   f"already current, {failed} failed, "
                   f"{evidence['skipped']} skipped.")
        if failed:
            bad = ", ".join(p["id"] for p in per_package
                            if p["status"] == "failed")[:200]
            return PackageResult(
                False, True, "upgrade_all", None, None, None, None,
                f"{summary} Failed: {bad}.",
                "", "", evidence, error="partial_failure")
        return PackageResult(
            True, True, "upgrade_all", None, None, None, None,
            summary, "", "", evidence, retryable=False)

    # -- experience memory (§13) -------------------------------------------
    @staticmethod
    def build_lesson(result: PackageResult) -> dict[str, str]:
        ev = result.evidence or {}
        op = result.operation or "package"
        pid = result.package_id or result.package or "?"
        if result.success:
            outcome = "success"
            if ev.get("already_current") or ev.get("already_installed"):
                lesson = f"{op} {pid}: already at requested state"
            elif op == "upgrade_all":
                lesson = (f"bulk update: {ev.get('successful', 0)} ok, "
                          f"{ev.get('failed', 0)} failed, "
                          f"{ev.get('skipped', 0)} skipped")
            else:
                lesson = (f"{op} {pid}: "
                          f"{result.previous_version} -> "
                          f"{result.new_version} verified")
            if ev.get("strategy"):
                lesson += f" via {ev['strategy']}"
        elif result.error in ("ambiguous_package", "package_not_found",
                              "not_installed"):
            outcome = "failure"
            lesson = f"{op} {pid}: {result.error}; ask, never guess"
        elif result.error == "verification_unavailable":
            outcome = "failure"
            lesson = f"{op} {pid}: unverified; never claim success"
        else:
            outcome = "failure"
            lesson = f"{op} {pid} failed: {result.error or 'unknown'}"
        return {"scenario": f"tool:{op}",
                "strategy": str(result.package or result.package_id
                                or op)[:300],
                "outcome": outcome, "lesson": lesson[:300]}
