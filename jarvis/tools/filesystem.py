"""
tools/filesystem.py

Filesystem tools for J.A.R.V.I.S.
Includes file list, inspect, create, edit, move, search, and delete.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
import logging

from backend.interfaces import ToolResult
from config.settings import PROJECT_ROOT

_log = logging.getLogger("jarvis.filesystem")


class FilesystemTools:

    @staticmethod
    def list_files(directory: str = ".") -> ToolResult:
        try:
            target = Path(directory).resolve()
            if not target.exists() or not target.is_dir():
                return ToolResult("list_files", False, f"Directory not found: {directory}", {"started": True, "completed": False})

            items = []
            for entry in target.iterdir():
                items.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(),
                    "size": entry.stat().st_size if entry.is_file() else 0,
                })

            return ToolResult("list_files", True, f"Listed {len(items)} items in {directory}", {"items": items, "started": True, "completed": True, "verified": True})
        except Exception as exc:
            return ToolResult("list_files", False, f"Failed to list directory: {exc}", {"started": True, "completed": False})

    @staticmethod
    def inspect_file(path: str, max_bytes: int = 10000) -> ToolResult:
        try:
            target = Path(path).resolve()
            if not target.exists() or not target.is_file():
                return ToolResult("inspect_file", False, f"File not found: {path}", {"started": True, "completed": False})

            with open(target, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_bytes)

            return ToolResult("inspect_file", True, f"Read {len(content)} chars from {target.name}", {"content": content, "size": target.stat().st_size, "started": True, "completed": True, "verified": True})
        except Exception as exc:
            return ToolResult("inspect_file", False, f"Failed to inspect file: {exc}", {"started": True, "completed": False})

    @staticmethod
    def create_file(path: str, content: str = "") -> ToolResult:
        try:
            target = Path(path).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            verified = target.exists() and target.read_text(encoding="utf-8") == content
            return ToolResult("create_file", True, f"Created file {target.name}", {"path": str(target), "verified": verified, "started": True, "completed": True})
        except Exception as exc:
            return ToolResult("create_file", False, f"Failed to create file: {exc}", {"started": True, "completed": False})

    @staticmethod
    def edit_file(path: str, content: str) -> ToolResult:
        try:
            target = Path(path).resolve()
            if not target.exists():
                return ToolResult("edit_file", False, f"File not found: {path}", {"started": True, "completed": False})
            target.write_text(content, encoding="utf-8")
            verified = target.read_text(encoding="utf-8") == content
            return ToolResult("edit_file", True, f"Updated file {target.name}", {"path": str(target), "verified": verified, "started": True, "completed": True})
        except Exception as exc:
            return ToolResult("edit_file", False, f"Failed to edit file: {exc}", {"started": True, "completed": False})

    @staticmethod
    def move_file(src: str, dst: str) -> ToolResult:
        try:
            src_path = Path(src).resolve()
            dst_path = Path(dst).resolve()
            if not src_path.exists():
                return ToolResult("move_file", False, f"Source file not found: {src}", {"started": True, "completed": False})
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_path), str(dst_path))
            verified = dst_path.exists() and not src_path.exists()
            return ToolResult("move_file", True, f"Moved {src_path.name} to {dst_path.name}", {"src": str(src_path), "dst": str(dst_path), "verified": verified, "started": True, "completed": True})
        except Exception as exc:
            return ToolResult("move_file", False, f"Failed to move file: {exc}", {"started": True, "completed": False})

    @staticmethod
    def search_files(directory: str, pattern: str) -> ToolResult:
        try:
            target = Path(directory).resolve()
            if not target.exists():
                return ToolResult("search_files", False, f"Directory not found: {directory}", {"started": True, "completed": False})

            matches = [str(p.relative_to(target)) for p in target.rglob(pattern) if p.is_file()][:50]
            return ToolResult("search_files", True, f"Found {len(matches)} matches for pattern '{pattern}'", {"matches": matches, "started": True, "completed": True, "verified": True})
        except Exception as exc:
            return ToolResult("search_files", False, f"Search failed: {exc}", {"started": True, "completed": False})

    @staticmethod
    def delete_file(path: str) -> ToolResult:
        try:
            target = Path(path).resolve()
            if not target.exists():
                return ToolResult("delete_file", False, f"File not found: {path}", {"started": True, "completed": False})

            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()

            verified = not target.exists()
            return ToolResult("delete_file", True, f"Deleted {target.name}", {"path": str(target), "verified": verified, "started": True, "completed": True})
        except Exception as exc:
            return ToolResult("delete_file", False, f"Delete failed: {exc}", {"started": True, "completed": False})
