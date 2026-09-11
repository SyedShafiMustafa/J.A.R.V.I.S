from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Optional

from .base import ParserBase, ParsedDocument


class ParserRegistry:
    """Registry that auto-discovers and registers all available parsers.

    Supports:
    - automatic registration of TXT/MD/PDF parsers
    - matching a file path to the appropriate parser
    - checking whether a path is supported without parsing
    - clear error for unsupported extensions
    """

    def __init__(self) -> None:
        self._parsers: list[ParserBase] = []
        self._extension_to_parser: dict[str, ParserBase] = {}
        self._init_parsers()

    def _init_parsers(self) -> None:
        """Discover and import all parsers in the parsers package."""
        from .base import ParserBase  # noqa: F401 - kept for reference
        from .text import TextParser
        from .markdown import MarkdownParser
        from .pdf import PdfParser

        # Register each parser
        for parser_class in [TextParser, MarkdownParser, PdfParser]:
            parser_instance = parser_class()
            self._parsers.append(parser_instance)
            for ext in parser_instance.supported_extensions:
                self._extension_to_parser[ext] = parser_instance

    def get_parser(self, path: str | Path) -> ParserBase:
        """Return the parser that can handle the given path.

        Raises ValueError if no parser supports the file extension.
        """
        path = Path(path)
        ext = path.suffix.lower()
        if ext not in self._extension_to_parser:
            raise ValueError(
                f"Unsupported file extension: {ext}. "
                f"Supported extensions: {self.supported_extensions()}"
            )
        return self._extension_to_parser[ext]

    def can_parse(self, path: str | Path) -> bool:
        """Return True if a parser supports the given file path."""
        path = Path(path)
        ext = path.suffix.lower()
        return ext in self._extension_to_parser

    def supported_extensions(self) -> tuple[str, ...]:
        """Return all supported file extensions."""
        exts: list[str] = []
        for parser in self._parsers:
            exts.extend(parser.supported_extensions)
        return tuple(exts)

    def parse(self, path: str | Path) -> ParsedDocument:
        """Parse the file at *path* using the matching parser."""
        parser = self.get_parser(path)
        return parser.parse(path)

    def list_supported(self) -> dict[str, type[ParserBase]]:
        """Return a dict mapping extensions to parser classes."""
        result: dict[str, type[ParserBase]] = {}
        for parser in self._parsers:
            for ext in parser.supported_extensions:
                result[ext] = parser.__class__
        return result