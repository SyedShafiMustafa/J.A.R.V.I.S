from __future__ import annotations

from typing import Optional

from ..parsers.base import ParsedDocument
from .base import ChunkerBase, ChunkerConfig
from .models import DocumentChunk


class PdfChunker(ChunkerBase):
    """Chunker for PDF documents with page metadata preservation.

    Preserves page information from PDF parsing. Prefers not to mix page
    metadata incorrectly across chunks. Retains page boundaries where practical.
    """

    @property
    def supported_file_types(self) -> tuple[str, ...]:
        return ("application/pdf",)

    def chunk(self, parsed_document: ParsedDocument) -> list[DocumentChunk]:
        """Chunk a PDF document respecting page boundaries.

        Args:
            parsed_document: ParsedDocument from PdfParser.

        Returns:
            Ordered list of DocumentChunk objects with page metadata.

        Raises:
            ValueError: If document text is empty.
        """
        if not parsed_document.text:
            raise ValueError("Cannot chunk empty document")

        text = parsed_document.text
        chunks: list[DocumentChunk] = []

        # Split into pages based on PDF metadata
        pages = self._split_into_pages(text, parsed_document.metadata)

        if not pages:
            # Handle edge case of whitespace-only content
            return []

        # Build chunks from pages respecting size constraints
        chunk_texts = self._build_chunks_from_pages(pages)

        # Create DocumentChunk objects with page metadata
        for index, (chunk_text, page_info) in enumerate(chunk_texts):
            if not chunk_text.strip():
                continue  # Skip empty chunks

            chunk_id = self._generate_chunk_id(
                parsed_document.source_path,
                index,
                chunk_text,
            )

            token_count = self._estimate_token_count(chunk_text)

            # Preserve source metadata and add page information
            chunk_metadata = {
                **parsed_document.metadata,
                "chunk_type": "pdf",
                "page_range": page_info.get("page_range"),
                "page_count": page_info.get("page_count"),
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

    def _split_into_pages(self, text: str, metadata: dict) -> list[dict]:
        """Split PDF text into pages based on parsing metadata.

        Uses the page information stored during PDF parsing to reconstruct
        page boundaries. Falls back to paragraph splitting if page metadata
        is incomplete.

        Args:
            text: Full PDF text.
            metadata: PDF parsing metadata including page information.

        Returns:
            List of page dicts with 'text', 'page_number', 'text_length'.
        """
        if not text:
            return []

        pages: list[dict] = []

        # Try to use page metadata from PDF parsing
        if "pages" in metadata and metadata["pages"]:
            # The PDF parser stores page information, but text is concatenated
            # We'll split by double newlines as a reasonable approximation
            # and assign page numbers based on metadata
            paragraphs = text.split("\n\n")
            page_count = metadata.get("page_count", len(paragraphs))

            # Distribute paragraphs across pages evenly
            paragraphs_per_page = max(1, len(paragraphs) // page_count) if page_count > 0 else 1

            for i in range(page_count):
                start_idx = i * paragraphs_per_page
                end_idx = start_idx + paragraphs_per_page if i < page_count - 1 else len(paragraphs)

                page_paragraphs = paragraphs[start_idx:end_idx]
                if page_paragraphs:
                    page_text = "\n\n".join(p.strip() for p in page_paragraphs if p.strip())
                    if page_text:
                        pages.append({
                            "text": page_text,
                            "page_number": i + 1,
                            "text_length": len(page_text),
                        })
        else:
            # Fallback: split by paragraphs if page metadata is unavailable
            paragraphs = text.split("\n\n")
            for i, para in enumerate(paragraphs):
                if para.strip():
                    pages.append({
                        "text": para.strip(),
                        "page_number": i + 1,
                        "text_length": len(para),
                    })

        return pages

    def _build_chunks_from_pages(
        self,
        pages: list[dict],
    ) -> list[tuple[str, dict]]:
        """Build chunks from pages respecting size and overlap.

        Merges pages into chunks that fit within chunk_size, applying
        overlap between chunks when needed. Preserves page information.

        Args:
            pages: List of page dicts.

        Returns:
            List of (chunk_text, page_info) tuples.
        """
        if not pages:
            return []

        chunks: list[tuple[str, dict]] = []
        current_chunk: list[dict] = []
        current_size = 0
        current_page_info: dict = {}

        for page in pages:
            page_text = page["text"]
            page_size = self._estimate_token_count(page_text)

            # If adding this page would exceed chunk size,
            # and we have content, start a new chunk
            if current_size + page_size > self.config.chunk_size and current_chunk:
                # Save current chunk
                chunk_text = "\n\n".join(p["text"] for p in current_chunk)
                if chunk_text.strip():
                    chunks.append((chunk_text, current_page_info))

                # Start new chunk with overlap if configured
                current_chunk = self._apply_overlap_pages(current_chunk)
                current_size = sum(
                    self._estimate_token_count(p["text"]) for p in current_chunk
                )
                current_page_info = current_chunk[0] if current_chunk else {}

            # Add page to current chunk
            current_chunk.append(page)
            current_size += page_size

            # Update page info for this chunk
            if not current_page_info:
                current_page_info = {
                    "page_range": [page["page_number"]],
                    "page_count": 1,
                }
            else:
                if "page_range" not in current_page_info:
                    current_page_info["page_range"] = []
                if page["page_number"] not in current_page_info["page_range"]:
                    current_page_info["page_range"].append(page["page_number"])
                current_page_info["page_count"] = len(current_page_info["page_range"])

        # Add final chunk if it has content
        if current_chunk:
            chunk_text = "\n\n".join(p["text"] for p in current_chunk)
            if chunk_text.strip():
                chunks.append((chunk_text, current_page_info))

        # Handle min_chunk_size by merging small chunks
        chunks = self._merge_small_chunks(chunks)

        return chunks

    def _apply_overlap_pages(
        self,
        current_chunk: list[dict],
    ) -> list[dict]:
        """Apply overlap by keeping last few pages from previous chunk.

        Args:
            current_chunk: Current chunk pages.

        Returns:
            Page dicts to include in next chunk (with overlap).
        """
        if self.config.overlap == 0 or not current_chunk:
            return []

        # Keep pages from the end that fit within overlap budget
        overlap_pages: list[dict] = []
        overlap_size = 0

        for page in reversed(current_chunk):
            page_size = self._estimate_token_count(page["text"])

            if overlap_size + page_size <= self.config.overlap:
                overlap_pages.insert(0, page)
                overlap_size += page_size
            else:
                break

        return overlap_pages

    def _merge_small_chunks(
        self,
        chunks: list[tuple[str, dict]],
    ) -> list[tuple[str, dict]]:
        """Merge chunks that are below min_chunk_size.

        Args:
            chunks: List of (chunk_text, page_info) tuples.

        Returns:
            List of merged (chunk_text, page_info) tuples.
        """
        if not chunks:
            return []

        merged: list[tuple[str, dict]] = []
        current_merged: list[str] = []
        current_size = 0
        current_page_info: dict = {}

        for chunk_text, page_info in chunks:
            chunk_size = self._estimate_token_count(chunk_text)

            # If this chunk is above minimum, flush current merged chunks
            if chunk_size >= self.config.min_chunk_size and current_merged:
                merged_text = "\n\n".join(current_merged)
                if merged_text.strip():
                    merged.append((merged_text, current_page_info))
                current_merged = []
                current_size = 0
                current_page_info = {}

            current_merged.append(chunk_text)
            current_size += chunk_size

            # Update page info (merge page ranges)
            if page_info.get("page_range"):
                if not current_page_info.get("page_range"):
                    current_page_info = {
                        "page_range": list(page_info["page_range"]),
                        "page_count": page_info.get("page_count", len(page_info["page_range"])),
                    }
                else:
                    # Merge page ranges
                    for page_num in page_info["page_range"]:
                        if page_num not in current_page_info["page_range"]:
                            current_page_info["page_range"].append(page_num)
                    current_page_info["page_count"] = len(current_page_info["page_range"])

        # Add final merged chunk
        if current_merged:
            merged_text = "\n\n".join(current_merged)
            if merged_text.strip():
                merged.append((merged_text, current_page_info))

        return merged
