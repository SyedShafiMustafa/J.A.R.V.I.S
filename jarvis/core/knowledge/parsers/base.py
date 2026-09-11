from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class ParsedDocument:
    """Structured document representation produced by parsers."""

    def __init__(
        self,
        source_path: str,
        title: str,
        text: str,
        file_type: str,
        metadata: Optional[dict] = None,
    ):
        self.source_path = source_path
        self.title = title
        self.text = text
        self.file_type = file_type
        self.metadata = metadata if metadata is not None else {}


class ParserBase(ABC):
    """Abstract base class for all document parsers."""

    @property
    @abstractmethod
    def supported_extensions(self) -> tuple[str, ...]:
        """File extensions this parser supports (lowercase, with dot)."""

    def can_parse(self, path: str | Path) -> bool:
        """Return True if this parser can handle the given file path."""
        ext = Path(path).suffix.lower()
        return ext in self.supported_extensions

    @abstractmethod
    def parse(self, path: str | Path) -> ParsedDocument:
        """Parse the file at *path* and return a ParsedDocument."""
        raise NotImplementedError