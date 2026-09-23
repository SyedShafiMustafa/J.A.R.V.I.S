"""
tools/organizer.py

Autonomous file-management tools for J.A.R.V.I.S.

Capabilities
------------
- analyze_directory : summarize a folder (counts, sizes, age, duplicates)
- organize_directory: build and (optionally) apply an organization plan
- find_duplicates   : content-hash duplicate detection

Every capability follows J.A.R.V.I.S.'s observe -> plan -> execute -> verify
pattern. A plan can always be previewed with a dry run before a single file
moves, and each executed move is verified after it happens (destination exists
and source is gone). Nothing is ever overwritten: name collisions get a
" (1)" suffix instead.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from backend.interfaces import ToolResult

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

CATEGORY_MAP: dict[str, set[str]] = {
    "Documents": {
        ".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".md", ".epub",
        ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp", ".csv", ".tsv",
    },
    "Images": {
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".tif",
        ".tiff", ".heic", ".heif", ".ico", ".raw",
    },
    "Video": {
        ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v",
        ".mpg", ".mpeg", ".3gp",
    },
    "Audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".oga", ".m4a", ".wma", ".opus"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".iso", ".cab"},
    "Code": {
        ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt",
        ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
        ".html", ".htm", ".css", ".scss", ".sass", ".json", ".yaml", ".yml",
        ".toml", ".ini", ".cfg", ".sh", ".bat", ".ps1", ".sql", ".xml", ".lua",
        ".pl", ".swift", ".vue", ".dart",
    },
    "Installers": {".exe", ".msi", ".msix", ".appx", ".dmg", ".pkg", ".deb", ".rpm", ".apk"},
}

_EXT_TO_CATEGORY: dict[str, str] = {
    ext: category for category, exts in CATEGORY_MAP.items() for ext in exts
}

STRATEGIES = ("by_type", "by_date", "by_year", "by_extension", "by_age")

# Folders an "already organized" tree will contain, so re-running does not
# shuffle files that are already in place.
_KNOWN_FOLDERS = set(CATEGORY_MAP) | {"Other", "Archive"}

# Friendly names a user says out loud -> real OS folder under the home dir.
_HOME_ALIASES: dict[str, str] = {
    "download": "Downloads",
    "downloads": "Downloads",
    "desktop": "Desktop",
    "document": "Documents",
    "documents": "Documents",
    "docs": "Documents",
    "picture": "Pictures",
    "pictures": "Pictures",
    "photo": "Pictures",
    "photos": "Pictures",
    "image": "Pictures",
    "images": "Pictures",
    "music": "Music",
    "songs": "Music",
    "video": "Videos",
    "videos": "Videos",
    "movie": "Videos",
    "movies": "Videos",
}

_TEMP_SUFFIXES = {".tmp", ".temp", ".crdownload", ".part", ".partial", ".download"}


def category_for(path: str | Path) -> str:
    """Return the human category for a file, or ``Other``."""
    return _EXT_TO_CATEGORY.get(Path(path).suffix.lower(), "Other")


def resolve_user_directory(value: str | None) -> Path | None:
    """Resolve a spoken / typed directory name to a real path.

    Organization work only ever touches folders that exist, so this is
    the must-exist view over the shared ``tools.filesystem``
    resolver — one resolution rule for the whole agent. Returns
    ``None`` when nothing trustworthy matches, so a caller never
    silently operates on the wrong folder.
    """
    from tools.filesystem import resolve_user_path
    return resolve_user_path(value, must_exist=True)


def _iter_files(base: Path, recursive: bool = False) -> Iterable[Path]:
    iterator = base.rglob("*") if recursive else base.iterdir()
    for entry in iterator:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        if entry.name.startswith("."):
            continue
        if entry.suffix.lower() in _TEMP_SUFFIXES:
            continue
        yield entry


def _file_hash(path: Path, chunk_size: int = 65536) -> str | None:
    digest = hashlib.sha1()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _unique_destination(dst: Path) -> Path:
    """Return ``dst`` or a non-colliding ``name (n).ext`` variant.

    Never returns an existing path, so an organize run can never overwrite a
    file that is already there.
    """
    if not dst.exists():
        return dst
    stem, suffix, parent = dst.stem, dst.suffix, dst.parent
    index = 1
    while True:
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _age_days(path: Path) -> float:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return 0.0
    return max((datetime.now(timezone.utc) - mtime).total_seconds() / 86400.0, 0.0)


class FileOrganizer:
    """Analyze, preview and safely reorganize directories."""

    # ------------------------------------------------------------------
    # read-only
    # ------------------------------------------------------------------

    def analyze_directory(self, directory: str = ".", recursive: bool = False) -> ToolResult:
        base = resolve_user_directory(directory)
        if base is None or not base.is_dir():
            return ToolResult(
                "analyze_directory",
                False,
                f"I couldn't find the folder '{directory}'.",
                {"started": True, "completed": False, "verified": False},
            )

        files = list(_iter_files(base, recursive))
        by_category: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "bytes": 0})
        total_bytes = 0
        mtimes: list[float] = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            category = category_for(path)
            by_category[category]["count"] += 1
            by_category[category]["bytes"] += stat.st_size
            total_bytes += stat.st_size
            mtimes.append(stat.st_mtime)

        duplicates = self._duplicate_groups(files)
        duplicate_files = sum(len(group["files"]) for group in duplicates)

        oldest = newest = None
        if mtimes:
            oldest = datetime.fromtimestamp(min(mtimes), tz=timezone.utc).isoformat()
            newest = datetime.fromtimestamp(max(mtimes), tz=timezone.utc).isoformat()

        return ToolResult(
            "analyze_directory",
            True,
            f"Analyzed {len(files)} files in {base.name}.",
            {
                "directory": str(base),
                "total_files": len(files),
                "total_bytes": total_bytes,
                "by_category": {k: dict(v) for k, v in sorted(by_category.items())},
                "oldest": oldest,
                "newest": newest,
                "duplicate_groups": len(duplicates),
                "duplicate_files": duplicate_files,
                "started": True,
                "completed": True,
                "verified": True,
            },
        )

    def find_duplicates(self, directory: str = ".", recursive: bool = False) -> ToolResult:
        base = resolve_user_directory(directory)
        if base is None or not base.is_dir():
            return ToolResult(
                "find_duplicates",
                False,
                f"I couldn't find the folder '{directory}'.",
                {"started": True, "completed": False, "verified": False},
            )

        groups = self._duplicate_groups(list(_iter_files(base, recursive)))
        wasted = sum(group["size"] * (len(group["files"]) - 1) for group in groups)
        message = (
            f"Found {len(groups)} duplicate group(s) in {base.name}, wasting {_human_bytes(wasted)}."
            if groups
            else f"No duplicate files found in {base.name}."
        )
        return ToolResult(
            "find_duplicates",
            True,
            message,
            {
                "directory": str(base),
                "groups": groups,
                "group_count": len(groups),
                "wasted_bytes": wasted,
                "started": True,
                "completed": True,
                "verified": True,
            },
        )

    # ------------------------------------------------------------------
    # planning
    # ------------------------------------------------------------------

    def build_plan(
        self,
        directory: str,
        strategy: str = "by_type",
        older_than_days: int | None = None,
        recursive: bool = False,
    ) -> dict[str, Any]:
        """Compute (but do not perform) the moves for a strategy."""
        base = resolve_user_directory(directory)
        if base is None or not base.is_dir():
            raise FileNotFoundError(f"folder not found: {directory}")
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy: {strategy}")
        if strategy == "by_age" and not older_than_days:
            raise ValueError("by_age requires older_than_days")

        moves: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        for path in _iter_files(base, recursive):
            folder = self._target_folder(path, base, strategy, older_than_days)
            if folder is None:
                skipped.append({"path": str(path), "reason": "older files only"})
                continue
            dst = base / folder / path.name
            if dst.parent == path.parent:
                skipped.append({"path": str(path), "reason": "already organized"})
                continue
            moves.append({"src": str(path), "dst": str(dst), "category": folder})

        moves.sort(key=lambda item: item["dst"].lower())
        return {
            "directory": str(base),
            "strategy": strategy,
            "recursive": recursive,
            "older_than_days": older_than_days,
            "moves": moves,
            "skipped": skipped,
        }

    @staticmethod
    def _target_folder(
        path: Path,
        base: Path,
        strategy: str,
        older_than_days: int | None,
    ) -> str | None:
        if strategy == "by_type":
            return category_for(path)
        if strategy == "by_extension":
            return path.suffix.lower().lstrip(".") or "no_extension"
        if strategy in ("by_date", "by_year"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                return "Undated"
            year, month = f"{mtime.year}", f"{mtime.month:02d}"
            return f"{year}/{month}" if strategy == "by_date" else year
        if strategy == "by_age":
            age = _age_days(path)
            if older_than_days is not None and age < older_than_days:
                return None
            try:
                year = datetime.fromtimestamp(path.stat().st_mtime).year
            except OSError:
                year = "Undated"
            return f"Archive/{year}"
        return None

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    def organize_directory(
        self,
        directory: str,
        strategy: str = "by_type",
        dry_run: bool = True,
        older_than_days: int | None = None,
        recursive: bool = False,
    ) -> ToolResult:
        try:
            plan = self.build_plan(directory, strategy, older_than_days, recursive)
        except (FileNotFoundError, ValueError) as exc:
            return ToolResult(
                "organize_directory",
                False,
                f"I couldn't build an organization plan: {exc}",
                {"started": True, "completed": False, "verified": False},
            )

        base_name = Path(plan["directory"]).name or plan["directory"]
        if not plan["moves"]:
            return ToolResult(
                "organize_directory",
                True,
                f"Nothing to organize in {base_name} — it's already tidy.",
                {
                    "dry_run": dry_run,
                    "moves": 0,
                    "plan": plan,
                    "started": True,
                    "completed": True,
                    "verified": True,
                },
            )

        if dry_run:
            preview = plan["moves"][:20]
            return ToolResult(
                "organize_directory",
                True,
                f"I can organize {len(plan['moves'])} files in {base_name} ({strategy}).",
                {
                    "dry_run": True,
                    "moves": len(plan["moves"]),
                    "preview": preview,
                    "plan": plan,
                    "started": True,
                    "completed": True,
                    "verified": True,
                },
            )

        moved, failures = self._apply(plan)
        verified = not failures and len(moved) == len(plan["moves"])
        if failures:
            message = (
                f"Organized {len(moved)} of {len(plan['moves'])} files in {base_name}; "
                f"{len(failures)} could not be moved."
            )
        else:
            message = f"Organized {len(moved)} files in {base_name}."
        return ToolResult(
            "organize_directory",
            verified,
            message,
            {
                "dry_run": False,
                "moves": len(moved),
                "moved": moved,
                "failures": failures,
                "verified": verified,
                "started": True,
                "completed": True,
            },
        )

    @staticmethod
    def _apply(plan: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        moved: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        for move in plan.get("moves") or []:
            src = Path(move["src"])
            dst = Path(move["dst"])
            try:
                if not src.exists():
                    failures.append({"src": str(src), "reason": "source missing"})
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                target = _unique_destination(dst)
                if target.resolve() == src.resolve():
                    continue
                shutil.move(str(src), str(target))
                if target.exists() and not src.exists():
                    moved.append({"src": str(src), "dst": str(target)})
                else:
                    failures.append({"src": str(src), "reason": "verification failed"})
            except Exception as exc:  # noqa: BLE001 - surfaced in the result
                failures.append({"src": str(src), "reason": str(exc)})
        return moved, failures

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _duplicate_groups(files: list[Path]) -> list[dict[str, Any]]:
        by_size: dict[int, list[Path]] = defaultdict(list)
        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > 0:
                by_size[size].append(path)

        groups: list[dict[str, Any]] = []
        for size, candidates in by_size.items():
            if len(candidates) < 2:
                continue
            by_hash: dict[str, list[Path]] = defaultdict(list)
            for path in candidates:
                digest = _file_hash(path)
                if digest:
                    by_hash[digest].append(path)
            for digest, cluster in by_hash.items():
                if len(cluster) > 1:
                    groups.append({
                        "hash": digest[:12],
                        "size": size,
                        "files": [str(p) for p in cluster],
                    })
        groups.sort(key=lambda g: g["size"] * (len(g["files"]) - 1), reverse=True)
        return groups


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


# ---------------------------------------------------------------------------
# Natural-language parsing (deterministic fast path)
# ---------------------------------------------------------------------------

_DIRECTORY_WORDS = (
    "downloads", "download", "desktop", "documents", "document", "docs",
    "pictures", "picture", "photos", "photo", "images", "image",
    "music", "videos", "video", "movies", "movie",
)

_VERB_PATTERN = r"(?:organi[sz]e|sort|tidy(?: up)?|clean ?up|declutter|arrange|archive)"

_UNIT_DAYS = {
    "day": 1, "days": 1, "week": 7, "weeks": 7,
    "month": 30, "months": 30, "year": 365, "years": 365,
}


def parse_organize_request(text: str) -> dict[str, Any] | None:
    """Parse a spoken/typed file-organization request, or return ``None``.

    Recognizes the folder, the strategy, and an optional age threshold, e.g.::

        "organize my downloads"                 -> by_type, preview first
        "sort my downloads by type"             -> by_type
        "clean up my documents by date"         -> by_date
        "archive files older than 6 months in downloads" -> by_age, 180 days
        "tidy downloads, go ahead"              -> by_type, execute directly
    """
    if not text:
        return None
    low = text.lower()
    if not re.search(_VERB_PATTERN, low):
        return None

    directory = None
    match = re.search(
        r"(?:in|inside|on|from|of)\s+(?:my\s+|the\s+)?(" + "|".join(_DIRECTORY_WORDS) + r")\b",
        low,
    )
    if not match:
        match = re.search(
            _VERB_PATTERN + r"\s+(?:my\s+|the\s+)?(" + "|".join(_DIRECTORY_WORDS) + r")\b",
            low,
        )
    if match:
        directory = match.group(1)
    else:
        named = re.search(r"(?:my|the)\s+([a-z0-9_\- ]{2,40}?)\s+(?:folder|directory)\b", low)
        if named:
            directory = named.group(1).strip()

    if not directory:
        return None

    strategy = "by_type"
    if "by extension" in low:
        strategy = "by_extension"
    elif "by date" in low or "by month" in low or "by month and year" in low:
        strategy = "by_date"
    elif "by year" in low:
        strategy = "by_year"

    older_than_days = None
    age_match = re.search(r"older than\s+(\d+)\s+(day|days|week|weeks|month|months|year|years)", low)
    if age_match:
        strategy = "by_age"
        older_than_days = int(age_match.group(1)) * _UNIT_DAYS[age_match.group(2)]

    explicit = any(
        phrase in low
        for phrase in (
            "go ahead", "do it", "just do it", "and move", "no need to ask",
            "don't ask", "dont ask", "carry on",
        )
    )

    return {
        "directory": directory,
        "strategy": strategy,
        "older_than_days": older_than_days,
        "explicit": explicit,
    }
