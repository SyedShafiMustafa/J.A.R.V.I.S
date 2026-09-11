from __future__ import annotations

from typing import Optional

from ..parsers.base import ParsedDocument
from .base import ChunkerBase, ChunkerConfig
from .markdown import MarkdownChunker
from .models import DocumentChunk
from .pdf import PdfChunker
from .text import TextChunker


class ChunkerRegistry:
    """Registry for document chunkers by file type.

    Routes parsed documents to the appropriate chunker based on file type.
    Provides a unified interface for chunking documents regardless of format.
    """

    def __init__(self, config: Optional[ChunkerConfig] = None):
        """Initialize chunker registry with optional default configuration.

        Args:
            config: Default configuration for all chunkers. If None, uses defaults.
        """
        self.config = config or ChunkerConfig()
        self._chunkers: dict[str, ChunkerBase] = {}

        # Register built-in chunkers
        self._register_builtin_chunkers()

    def _register_builtin_chunkers(self):
        """Register the built-in chunkers for supported file types."""
        self.register(TextChunker(self.config))
        self.register(MarkdownChunker(self.config))
        self.register(PdfChunker(self.config))

    def register(self, chunker: ChunkerBase):
        """Register a chunker for its supported file types.

        Args:
            chunker: Chunker instance to register.

        Raises:
            ValueError: If chunker has no supported file types.
        """
        if not chunker.supported_file_types:
            raise ValueError("Chunker must support at least one file type")

        for file_type in chunker.supported_file_types:
            self._chunkers[file_type] = chunker

    def supported_file_types(self) -> tuple[str, ...]:
        """Return all supported file types.

        Returns:
            Tuple of MIME type strings.
        """
        return tuple(self._chunkers.keys())

    def can_chunk(self, file_type: str) -> bool:
        """Check if a file type is supported for chunking.

        Args:
            file_type: MIME type string.

        Returns:
            True if supported, False otherwise.
        """
        return file_type in self._chunkers

    def get_chunker(self, file_type: str) -> ChunkerBase:
        """Get the appropriate chunker for a file type.

        Args:
            file_type: MIME type string.

        Returns:
            Chunker instance for the file type.

        Raises:
            ValueError: If file type is not supported.
        """
        if file_type not in self._chunkers:
            raise ValueError(
                f"Unsupported file type for chunking: {file_type}. "
                f"Supported types: {self.supported_file_types()}"
            )
        return self._chunkers[file_type]

    def chunk(self, parsed_document: ParsedDocument) -> list[DocumentChunk]:
        """Chunk a parsed document using the appropriate chunker.

        Args:
            parsed_document: ParsedDocument from a parser.

        Returns:
            Ordered list of DocumentChunk objects.

        Raises:
            ValueError: If file type is not supported.
        """
        chunker = self.get_chunker(parsed_document.file_type)
        return chunker.chunk(parsed_document)
