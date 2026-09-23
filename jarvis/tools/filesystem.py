"""
tools/filesystem.py

Filesystem tools for J.A.R.V.I.S.
Includes file list, inspect, create, edit, move, search, and delete.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
import logging

from backend.interfaces import ToolResult
from config.settings import PROJECT_ROOT

_log = logging.getLogger("jarvis.filesystem")

# Spoken folder names -> real folders under the user home. Shared by the
# filesystem tools and the organizer so "my Downloads folder" resolves to
# the same place everywhere (previously the two paths disagreed).
KNOWN_FOLDER_ALIASES: dict[str, str] = {
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
    "home": "",
}

_FILLER_WORDS = ("my", "the", "folder", "folders", "directory",
                 "directories", "files", "file")


def resolve_user_path(value: str | None,
                      must_exist: bool = False) -> Path | None:
    """Resolve spoken/typed paths predictably.

    Order: explicit path shapes (absolute, ``~``, env vars, anything
    with a slash) first; then known-folder aliases ("my Downloads
    folder", "home"); then CWD-relative fallback. Returns ``None``
    when nothing trustworthy matches (``must_exist`` additionally
    requires the target to exist), so callers never silently operate
    on the wrong folder.
    """
    if not value:
        return None
    raw = str(value).strip().strip("\"'")
    if not raw:
        return None

    expanded = os.path.expandvars(os.path.expanduser(raw))
    explicit_shape = (
        expanded != raw
        or os.path.isabs(expanded)
        or "/" in raw
        or "\\" in raw
        or raw.startswith("~")
        or re.match(r"^[A-Za-z]:", raw) is not None
    )
    if explicit_shape:
        # A spoken path rooted at a known folder ("Desktop/report.pdf",
        # "my Downloads/x.zip") resolves under home, not CWD — the
        # planner emits these shapes constantly.
        first, _, rest = raw.replace("\\", "/").partition("/")
        if rest:
            head = re.sub(r"^(my|the)\s+", "", first.strip().lower())
            alias = KNOWN_FOLDER_ALIASES.get(head)
            if alias is not None:
                base = Path.home() / alias if alias else Path.home()
                try:
                    resolved = (base / rest).resolve()
                except OSError:
                    return None
                if must_exist and not resolved.exists():
                    return None
                return resolved
        try:
            resolved = Path(expanded).resolve()
        except OSError:
            return None
        if must_exist and not resolved.exists():
            return None
        return resolved

    key = re.sub(r"\b(" + "|".join(_FILLER_WORDS) + r")\b", " ",
                 raw.lower())
    key = re.sub(r"[^a-z0-9]+", "", key)
    if key in KNOWN_FOLDER_ALIASES:
        name = KNOWN_FOLDER_ALIASES[key]
        target = Path.home() / name if name else Path.home()
        if must_exist and not target.exists():
            return None
        try:
            return target.resolve()
        except OSError:
            return target

    try:
        fallback = Path(raw).resolve()
    except OSError:
        return None
    if must_exist and not fallback.exists():
        # Bare filenames from recent turns ("move report.pdf") are
        # usually sitting in a known folder, not CWD: search Desktop,
        # Downloads and Documents for the exact name before giving up.
        # Fixed order + exact match keep it predictable; the resolved
        # path is always reported back truthfully.
        if "/" not in raw and "\\" not in raw:
            for folder in ("Desktop", "Downloads", "Documents"):
                try:
                    candidate = (Path.home() / folder / raw).resolve()
                except OSError:
                    continue
                if candidate.exists():
                    return candidate
        return None
    return fallback


def display_path(path: str | Path) -> str:
    """Short ``~/Downloads``-style rendering for user messages."""
    text = str(path)
    try:
        home = str(Path.home())
        if text == home:
            return "~"
        if text.startswith(home + os.sep):
            return "~" + text[len(home):]
    except Exception:
        pass
    return text


class FilesystemTools:

    @staticmethod
    def list_files(directory: str = ".") -> ToolResult:
        try:
            target = resolve_user_path(directory, must_exist=True)
            if target is None or not target.is_dir():
                return ToolResult("list_files", False, f"Directory not found: {display_path(directory)}", {"started": True, "completed": False})

            items = []
            for entry in target.iterdir():
                items.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(),
                    "size": entry.stat().st_size if entry.is_file() else 0,
                })

            return ToolResult("list_files", True, f"Listed {len(items)} items in {display_path(target)}", {"items": items, "started": True, "completed": True, "verified": True})
        except Exception as exc:
            return ToolResult("list_files", False, f"Failed to list directory: {exc}", {"started": True, "completed": False})

    @staticmethod
    def inspect_file(path: str, max_bytes: int = 10000) -> ToolResult:
        try:
            target = resolve_user_path(path, must_exist=True)
            if target is None or not target.is_file():
                return ToolResult("inspect_file", False, f"File not found: {display_path(path)}", {"started": True, "completed": False})

            with open(target, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_bytes)

            return ToolResult("inspect_file", True, f"Read {len(content)} chars from {target.name}", {"content": content, "size": target.stat().st_size, "started": True, "completed": True, "verified": True})
        except Exception as exc:
            return ToolResult("inspect_file", False, f"Failed to inspect file: {exc}", {"started": True, "completed": False})

    @staticmethod
    def create_file(path: str, content: str = "") -> ToolResult:
        try:
            target = resolve_user_path(path)
            if target is None:
                return ToolResult("create_file", False, f"Cannot resolve path: {display_path(path)}", {"started": True, "completed": False})
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            verified = target.exists() and target.read_text(encoding="utf-8") == content
            if not verified:
                return ToolResult("create_file", False, f"Write to {display_path(target)} could not be verified", {"path": str(target), "verified": False, "started": True, "completed": False})
            return ToolResult("create_file", True, f"Created {display_path(target)}", {"path": str(target), "verified": True, "started": True, "completed": True})
        except Exception as exc:
            return ToolResult("create_file", False, f"Failed to create file: {exc}", {"started": True, "completed": False})

    @staticmethod
    def edit_file(path: str, content: str) -> ToolResult:
        try:
            target = resolve_user_path(path, must_exist=True)
            if target is None or not target.exists():
                return ToolResult("edit_file", False, f"File not found: {display_path(path)}", {"started": True, "completed": False})
            target.write_text(content, encoding="utf-8")
            verified = target.read_text(encoding="utf-8") == content
            if not verified:
                return ToolResult("edit_file", False, f"Update to {display_path(target)} could not be verified", {"path": str(target), "verified": False, "started": True, "completed": False})
            return ToolResult("edit_file", True, f"Updated {display_path(target)}", {"path": str(target), "verified": True, "started": True, "completed": True})
        except Exception as exc:
            return ToolResult("edit_file", False, f"Failed to edit file: {exc}", {"started": True, "completed": False})

    @staticmethod
    def move_file(src: str, dst: str) -> ToolResult:
        try:
            src_path = resolve_user_path(src, must_exist=True)
            dst_path = resolve_user_path(dst)
            if src_path is None or not src_path.exists():
                return ToolResult("move_file", False, f"Source file not found: {display_path(src)}", {"started": True, "completed": False})
            if dst_path is None:
                return ToolResult("move_file", False, f"Cannot resolve destination: {display_path(dst)}", {"started": True, "completed": False})
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_path), str(dst_path))
            verified = dst_path.exists() and not src_path.exists()
            if not verified:
                return ToolResult("move_file", False, f"Move to {display_path(dst_path)} could not be verified", {"src": str(src_path), "dst": str(dst_path), "verified": False, "started": True, "completed": False})
            return ToolResult("move_file", True, f"Moved {display_path(src_path)} to {display_path(dst_path)}", {"src": str(src_path), "dst": str(dst_path), "verified": True, "started": True, "completed": True})
        except Exception as exc:
            return ToolResult("move_file", False, f"Failed to move file: {exc}", {"started": True, "completed": False})

    @staticmethod
    def search_files(directory: str, pattern: str) -> ToolResult:
        try:
            target = resolve_user_path(directory, must_exist=True)
            if target is None or not target.exists():
                return ToolResult("search_files", False, f"Directory not found: {display_path(directory)}", {"started": True, "completed": False})

            matches = [str(p.relative_to(target)) for p in target.rglob(pattern) if p.is_file()][:50]
            return ToolResult("search_files", True, f"Found {len(matches)} matches for '{pattern}' in {display_path(target)}", {"matches": matches, "started": True, "completed": True, "verified": True})
        except Exception as exc:
            return ToolResult("search_files", False, f"Search failed: {exc}", {"started": True, "completed": False})

    @staticmethod
    def delete_file(path: str) -> ToolResult:
        try:
            target = resolve_user_path(path, must_exist=True)
            if target is None or not target.exists():
                return ToolResult("delete_file", False, f"File not found: {display_path(path)}", {"started": True, "completed": False})

            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()

            verified = not target.exists()
            if not verified:
                return ToolResult("delete_file", False, f"Delete of {display_path(target)} could not be verified", {"path": str(target), "verified": False, "started": True, "completed": False})
            return ToolResult("delete_file", True, f"Deleted {display_path(target)}", {"path": str(target), "verified": True, "started": True, "completed": True})
        except Exception as exc:
            return ToolResult("delete_file", False, f"Delete failed: {exc}", {"started": True, "completed": False})
