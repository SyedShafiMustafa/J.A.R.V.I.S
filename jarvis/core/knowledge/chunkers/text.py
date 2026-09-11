from __future__ import annotations

import re
from typing import Optional

from ..parsers.base import ParsedDocument
from .base import ChunkerBase, ChunkerConfig
from .models import DocumentChunk


class TextChunker(ChunkerBase):
    """Chunker for plain text documents.

    Prioritizes paragraph and sentence boundaries over arbitrary character splitting.
    Preserves document metadata and treats content as untrusted data.
    """

    @property
    def supported_file_types(self) -> tuple[str, ...]:
        return ("text/plain",)

    def chunk(self, parsed_document: ParsedDocument) -> list[DocumentChunk]:
        """Chunk a plain text document respecting paragraph boundaries.

        Args:
            parsed_document: ParsedDocument from TextParser.

        Returns:
            Ordered list of DocumentChunk objects.

        Raises:
            ValueError: If document text is empty.
        """
        if not parsed_document.text:
            raise ValueError("Cannot chunk empty document")

        text = parsed_document.text
        chunks: list[DocumentChunk] = []

        # Split into paragraphs first (respect paragraph boundaries)
        paragraphs = self._split_into_paragraphs(text)

        if not paragraphs:
            # Handle edge case of whitespace-only content
            return []

        # Build chunks from paragraphs respecting size constraints
        chunk_texts = self._build_chunks_from_paragraphs(paragraphs)

        # Create DocumentChunk objects
        for index, chunk_text in enumerate(chunk_texts):
            if not chunk_text.strip():
                continue  # Skip empty chunks

            chunk_id = self._generate_chunk_id(
                parsed_document.source_path,
                index,
                chunk_text,
            )

            token_count = self._estimate_token_count(chunk_text)

            # Preserve source metadata
            chunk_metadata = {
                **parsed_document.metadata,
                "chunk_type": "text",
            }

            chunk = DocumentChunk(
                chunk_id=chunk_id,
                source_path=parsed_document.source_path,
                document_title=parsed_document.title,
                file_type=parsed_document.file_type,
                chunk_index=index,
                content=chunk_text,
                token_count=token_count,
                metadata=chunk_metadata,
            )

            chunks.append(chunk)

        return chunks

    def _split_into_paragraphs(self, text: str) -> list[str]:
        """Split text into paragraphs while preserving structure.

        Uses double newlines as paragraph separators, falling back to single
        newlines if no double newlines exist.

        Args:
            text: Input text.

        Returns:
            List of paragraph strings.
        """
        if not text:
            return []

        # Try double newline split first (standard paragraph separation)
        paragraphs = text.split("\n\n")

        # If only one paragraph, try single newline split
        if len(paragraphs) == 1:
            paragraphs = text.split("\n")

        # Clean up whitespace in each paragraph
        cleaned_paragraphs = [p.strip() for p in paragraphs if p.strip()]

        return cleaned_paragraphs

    def _build_chunks_from_paragraphs(
        self,
        paragraphs: list[str],
    ) -> list[str]:
        """Build chunks from paragraphs respecting size and overlap.

        Merges paragraphs into chunks that fit within chunk_size, applying
        overlap between chunks when needed.

        Args:
            paragraphs: List of paragraph strings.

        Returns:
            List of chunk text strings.
        """
        if not paragraphs:
            return []

        chunks: list[str] = []
        current_chunk: list[str] = []
        current_size = 0

        for paragraph in paragraphs:
            paragraph_size = self._estimate_token_count(paragraph)

            # If adding this paragraph would exceed chunk size,
            # and we have content, start a new chunk
            if current_size + paragraph_size > self.config.chunk_size and current_chunk:
                # Save current chunk
                chunk_text = "\n\n".join(current_chunk)
                if chunk_text.strip():
                    chunks.append(chunk_text)

                # Start new chunk with overlap if configured
                current_chunk = self._apply_overlap(current_chunk)
                current_size = sum(
                    self._estimate_token_count(p) for p in current_chunk
                )

            # Add paragraph to current chunk
            current_chunk.append(paragraph)
            current_size += paragraph_size

        # Add final chunk if it has content
        if current_chunk:
            chunk_text = "\n\n".join(current_chunk)
            if chunk_text.strip():
                chunks.append(chunk_text)

        # Handle min_chunk_size by merging small chunks
        chunks = self._merge_small_chunks(chunks)

        return chunks

    def _apply_overlap(self, paragraphs: list[str]) -> list[str]:
        """Apply overlap by keeping last few paragraphs from previous chunk.

        Args:
            paragraphs: Current chunk paragraphs.

        Returns:
            Paragraphs to include in next chunk (with overlap).
        """
        if self.config.overlap == 0:
            return []

        # Keep paragraphs from the end that fit within overlap budget
        overlap_paragraphs: list[str] = []
        overlap_size = 0

        for paragraph in reversed(paragraphs):
            paragraph_size = self._estimate_token_count(paragraph)

            if overlap_size + paragraph_size <= self.config.overlap:
                overlap_paragraphs.insert(0, paragraph)
                overlap_size += paragraph_size
            else:
                break

        return overlap_paragraphs

    def _merge_small_chunks(self, chunks: list[str]) -> list[str]:
        """Merge chunks that are below min_chunk_size.

        Args:
            chunks: List of chunk strings.

        Returns:
            List of merged chunks.
        """
        if not chunks:
            return []

        merged: list[str] = []
        current_merged: list[str] = []
        current_size = 0

        for chunk in chunks:
            chunk_size = self._estimate_token_count(chunk)

            # If this chunk is above minimum, flush current merged chunks
            if chunk_size >= self.config.min_chunk_size and current_merged:
                merged_text = "\n\n".join(current_merged)
                if merged_text.strip():
                    merged.append(merged_text)
                current_merged = []
                current_size = 0

            current_merged.append(chunk)
            current_size += chunk_size

        # Add final merged chunk
        if current_merged:
            merged_text = "\n\n".join(current_merged)
            if merged_text.strip():
                merged.append(merged_text)

        return merged
