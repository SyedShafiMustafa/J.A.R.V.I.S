from __future__ import annotations

import warnings
from pathlib import Path

from .base import ParserBase, ParsedDocument


class TextParser(ParserBase):
    """Parser for plain .txt files.

    - UTF-8 first, then latin-1 fallback with explicit status
    - empty files handled correctly
    - invalid UTF-8 exposure via metadata (no silent acceptance)
    - never execute file contents
    """

    @property
    def supported_extensions(self) -> tuple[str, ...]:
        return (".txt",)

    def parse(self, path: str | Path) -> ParsedDocument:
        path = Path(path)
        source_path = str(path.resolve())

        # Attempt UTF-8 first
        text: str
        encoding: str = "utf-8"
        utf8_fallback: bool = False

        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except UnicodeDecodeError:
            # UTF-8 failed - use latin-1 fallback but expose it
            warnings.warn(
                f"File {path} is not valid UTF-8; falling back to latin-1 encoding.",
                stacklevel=2,
            )
            encoding = "latin-1"
            utf8_fallback = True
            with open(path, "r", encoding="latin-1") as f:
                text = f.read()

        # Derive title from filename (stem) for text files
        title = path.stem if path.stem else "untitled"

        # Byte count from raw bytes for metadata
        byte_count = len(open(source_path, "rb").read()) if path.exists() else 0

        file_type = "text/plain"

        metadata = {
            "encoding": encoding,
            "utf8_fallback": utf8_fallback,
            "byte_count": byte_count,
        }

        return ParsedDocument(
            source_path=source_path,
            title=title,
            text=text,
            file_type=file_type,
            metadata=metadata,
        )