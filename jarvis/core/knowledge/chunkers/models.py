from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DocumentChunk:
    """A chunk of document content with metadata for retrieval.

    Designed to be independent of embeddings, vector stores, and LLMs.
    Contains deterministic identifiers and rich metadata for future RAG systems.
    """

    chunk_id: str
    """Deterministic chunk identifier (hash-based or sequential)."""

    source_path: str
    """Original file path of the source document."""

    document_title: str
    """Title of the source document."""

    file_type: str
    """MIME type of the source file (e.g., 'text/plain', 'application/pdf')."""

    chunk_index: int
    """Zero-based index of this chunk in the document."""

    content: str
    """The text content of this chunk."""

    token_count: int
    """Token count or estimated token count for this chunk."""

    metadata: dict = field(default_factory=dict)
    """Additional metadata including source document metadata preserved from parsing."""

    def __post_init__(self):
        """Validate chunk fields after initialization."""
        if not self.chunk_id:
            raise ValueError("chunk_id cannot be empty")
        if not self.source_path:
            raise ValueError("source_path cannot be empty")
        if not self.content:
            raise ValueError("content cannot be empty")
        if self.chunk_index < 0:
            raise ValueError("chunk_index must be non-negative")
        if self.token_count < 0:
            raise ValueError("token_count must be non-negative")

    def to_dict(self) -> dict:
        """Convert chunk to dictionary for serialization."""
        return {
            "chunk_id": self.chunk_id,
            "source_path": self.source_path,
            "document_title": self.document_title,
            "file_type": self.file_type,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "token_count": self.token_count,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DocumentChunk":
        """Create chunk from dictionary for deserialization."""
        return cls(
            chunk_id=data["chunk_id"],
            source_path=data["source_path"],
            document_title=data["document_title"],
            file_type=data["file_type"],
            chunk_index=data["chunk_index"],
            content=data["content"],
            token_count=data["token_count"],
            metadata=data.get("metadata", {}),
        )
