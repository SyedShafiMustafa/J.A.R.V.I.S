from __future__ import annotations

import re
from typing import Optional

from ..parsers.base import ParsedDocument
from .base import ChunkerBase, ChunkerConfig
from .models import DocumentChunk


class MarkdownChunker(ChunkerBase):
    """Chunker for Markdown documents with heading preservation.

    Preserves heading hierarchy and metadata. Tries to keep headings with
    their associated content unless chunk size constraints require splitting.
    Carries heading metadata into each generated chunk.
    """

    @property
    def supported_file_types(self) -> tuple[str, ...]:
        return ("text/markdown",)

    def chunk(self, parsed_document: ParsedDocument) -> list[DocumentChunk]:
        """Chunk a Markdown document respecting heading boundaries.

        Args:
            parsed_document: ParsedDocument from MarkdownParser.

        Returns:
            Ordered list of DocumentChunk objects with heading metadata.

        Raises:
            ValueError: If document text is empty.
        """
        if not parsed_document.text:
            raise ValueError("Cannot chunk empty document")

        text = parsed_document.text
        chunks: list[DocumentChunk] = []

        # Split into sections based on headings
        sections = self._split_into_sections(text)

        if not sections:
            # Handle edge case of whitespace-only content
            return []

        # Build chunks from sections respecting size constraints
        chunk_texts = self._build_chunks_from_sections(sections)

        # Create DocumentChunk objects with heading metadata
        for index, (chunk_text, heading_info) in enumerate(chunk_texts):
            if not chunk_text.strip():
                continue  # Skip empty chunks

            chunk_id = self._generate_chunk_id(
                parsed_document.source_path,
                index,
                chunk_text,
            )

            token_count = self._estimate_token_count(chunk_text)

            # Preserve source metadata and add heading information
            chunk_metadata = {
                **parsed_document.metadata,
                "chunk_type": "markdown",
                "heading": heading_info.get("heading"),
                "heading_level": heading_info.get("level"),
                "section_path": heading_info.get("section_path"),
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

    def _split_into_sections(self, text: str) -> list[dict]:
        """Split Markdown text into sections based on headings.

        Each section contains the heading line and its content until the next
        heading of equal or higher level.

        Args:
            text: Markdown text.

        Returns:
            List of section dicts with 'text', 'heading', 'level', 'section_path'.
        """
        if not text:
            return []

        sections: list[dict] = []
        lines = text.splitlines()

        current_section: list[str] = []
        current_heading: Optional[str] = None
        current_level: int = 0
        section_path: list[str] = []

        for line in lines:
            # Check if this line is a heading
            heading_match = re.match(r"^(#{1,6})\s+(.+)$", line.strip())

            if heading_match:
                # Save previous section if it has content
                if current_section:
                    section_text = "\n".join(current_section).strip()
                    if section_text:
                        sections.append({
                            "text": section_text,
                            "heading": current_heading,
                            "level": current_level,
                            "section_path": list(section_path),
                        })

                # Start new section
                hashes = heading_match.group(1)
                heading_text = heading_match.group(2)
                level = len(hashes)

                # Update section path (maintain hierarchy)
                if level <= len(section_path):
                    section_path = section_path[:level - 1]
                while len(section_path) < level - 1:
                    section_path.append("...")
                section_path.append(heading_text)

                current_section = [line]
                current_heading = heading_text
                current_level = level
            else:
                current_section.append(line)

        # Add final section
        if current_section:
            section_text = "\n".join(current_section).strip()
            if section_text:
                sections.append({
                    "text": section_text,
                    "heading": current_heading,
                    "level": current_level,
                    "section_path": list(section_path),
                })

        return sections

    def _build_chunks_from_sections(
        self,
        sections: list[dict],
    ) -> list[tuple[str, dict]]:
        """Build chunks from sections respecting size and overlap.

        Merges sections into chunks that fit within chunk_size, applying
        overlap between chunks when needed. Preserves heading information.

        Args:
            sections: List of section dicts.

        Returns:
            List of (chunk_text, heading_info) tuples.
        """
        if not sections:
            return []

        chunks: list[tuple[str, dict]] = []
        current_chunk: list[dict] = []
        current_size = 0
        current_heading_info: dict = {}

        for section in sections:
            section_text = section["text"]
            section_size = self._estimate_token_count(section_text)

            # If adding this section would exceed chunk size,
            # and we have content, start a new chunk
            if current_size + section_size > self.config.chunk_size and current_chunk:
                # Save current chunk
                chunk_text = "\n\n".join(s["text"] for s in current_chunk)
                if chunk_text.strip():
                    chunks.append((chunk_text, current_heading_info))

                # Start new chunk with overlap if configured
                current_chunk = self._apply_overlap_sections(current_chunk)
                current_size = sum(
                    self._estimate_token_count(s["text"]) for s in current_chunk
                )
                current_heading_info = current_chunk[0] if current_chunk else {}

            # Add section to current chunk
            current_chunk.append(section)
            current_size += section_size

            # Update heading info for this chunk
            if section["heading"]:
                current_heading_info = {
                    "heading": section["heading"],
                    "level": section["level"],
                    "section_path": section["section_path"],
                }

        # Add final chunk if it has content
        if current_chunk:
            chunk_text = "\n\n".join(s["text"] for s in current_chunk)
            if chunk_text.strip():
                chunks.append((chunk_text, current_heading_info))

        # Handle min_chunk_size by merging small chunks
        chunks = self._merge_small_chunks(chunks)

        return chunks

    def _apply_overlap_sections(
        self,
        current_chunk: list[dict],
    ) -> list[dict]:
        """Apply overlap by keeping last few sections from previous chunk.

        Args:
            current_chunk: Current chunk sections.

        Returns:
            Section dicts to include in next chunk (with overlap).
        """
        if self.config.overlap == 0 or not current_chunk:
            return []

        # Keep sections from the end that fit within overlap budget
        overlap_sections: list[dict] = []
        overlap_size = 0

        for section in reversed(current_chunk):
            section_size = self._estimate_token_count(section["text"])

            if overlap_size + section_size <= self.config.overlap:
                overlap_sections.insert(0, section)
                overlap_size += section_size
            else:
                break

        return overlap_sections

    def _merge_small_chunks(
        self,
        chunks: list[tuple[str, dict]],
    ) -> list[tuple[str, dict]]:
        """Merge chunks that are below min_chunk_size.

        Args:
            chunks: List of (chunk_text, heading_info) tuples.

        Returns:
            List of merged (chunk_text, heading_info) tuples.
        """
        if not chunks:
            return []

        merged: list[tuple[str, dict]] = []
        current_merged: list[str] = []
        current_size = 0
        current_heading_info: dict = {}

        for chunk_text, heading_info in chunks:
            chunk_size = self._estimate_token_count(chunk_text)

            # If this chunk is above minimum, flush current merged chunks
            if chunk_size >= self.config.min_chunk_size and current_merged:
                merged_text = "\n\n".join(current_merged)
                if merged_text.strip():
                    merged.append((merged_text, current_heading_info))
                current_merged = []
                current_size = 0
                current_heading_info = {}

            current_merged.append(chunk_text)
            current_size += chunk_size

            # Update heading info (prefer the first meaningful heading)
            if heading_info.get("heading") and not current_heading_info.get("heading"):
                current_heading_info = heading_info

        # Add final merged chunk
        if current_merged:
            merged_text = "\n\n".join(current_merged)
            if merged_text.strip():
                merged.append((merged_text, current_heading_info))

        return merged
