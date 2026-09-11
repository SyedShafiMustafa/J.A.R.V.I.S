from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..parsers.base import ParsedDocument
from .models import DocumentChunk


class ChunkerConfig:
    """Configuration for document chunking behavior.

    Sensible defaults are provided for immediate use.
    """

    def __init__(
        self,
        chunk_size: int = 1000,
        overlap: int = 100,
        min_chunk_size: int = 50,
    ):
        """Initialize chunker configuration.

        Args:
            chunk_size: Target chunk size in tokens. Default: 1000 tokens.
            overlap: Number of tokens to overlap between chunks. Default: 100 tokens.
            min_chunk_size: Minimum chunk size in tokens. Smaller chunks are merged.
                          Default: 50 tokens.

        Raises:
            ValueError: If configuration values are invalid.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if overlap < 0:
            raise ValueError("overlap must be non-negative")
        if min_chunk_size < 0:
            raise ValueError("min_chunk_size must be non-negative")
        if overlap >= chunk_size:
            raise ValueError("overlap must be less than chunk_size")
        if min_chunk_size >= chunk_size:
            raise ValueError("min_chunk_size must be less than chunk_size")

        self.chunk_size = chunk_size
        self.overlap = overlap
        self.min_chunk_size = min_chunk_size


class ChunkerBase(ABC):
    """Abstract base class for document chunkers.

    Concrete implementations handle specific file types with specialized
    metadata preservation (e.g., PDF pages, Markdown headings).
    """

    def __init__(self, config: Optional[ChunkerConfig] = None):
        """Initialize chunker with optional configuration.

        Args:
            config: Chunker configuration. If None, uses defaults.
        """
        self.config = config or ChunkerConfig()

    @property
    @abstractmethod
    def supported_file_types(self) -> tuple[str, ...]:
        """MIME types this chunker supports."""

    def can_chunk(self, file_type: str) -> bool:
        """Return True if this chunker can handle the given file type."""
        return file_type in self.supported_file_types

    @abstractmethod
    def chunk(self, parsed_document: ParsedDocument) -> list[DocumentChunk]:
        """Chunk a parsed document into DocumentChunk objects.

        Args:
            parsed_document: ParsedDocument from a parser.

        Returns:
            Ordered list of DocumentChunk objects.

        Raises:
            ValueError: If the document cannot be chunked.
        """
        raise NotImplementedError

    def _estimate_token_count(self, text: str) -> int:
        """Estimate token count using character-based heuristic.

        Uses ~4 characters per token as a rough estimate for English text.
        This is a conservative estimate suitable for planning chunk sizes.

        Args:
            text: Text to estimate token count for.

        Returns:
            Estimated token count.
        """
        if not text:
            return 0
        # Conservative estimate: ~4 characters per token
        return len(text) // 4

    def _generate_chunk_id(
        self,
        source_path: str,
        chunk_index: int,
        content: str,
    ) -> str:
        """Generate deterministic chunk identifier.

        Uses a hash-based approach for deterministic IDs that incorporate
        source path, index, and content hash.

        Args:
            source_path: Original file path.
            chunk_index: Chunk index in document.
            content: Chunk content.

        Returns:
            Deterministic chunk identifier string.
        """
        import hashlib

        # Create hash from source path and chunk index for determinism
        path_hash = hashlib.md5(source_path.encode()).hexdigest()[:8]
        content_hash = hashlib.md5(content.encode()).hexdigest()[:8]

        return f"{path_hash}_{chunk_index}_{content_hash}"
