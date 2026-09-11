from __future__ import annotations

import logging
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .base import ParserBase, ParsedDocument

logger = logging.getLogger(__name__)


class PdfParser(ParserBase):
    """Parser for .pdf files using pypdf (mature local parser).

    - extract text page-by-page
    - preserve page numbers in metadata
    - provide source traceability for future RAG citations
    - fail cleanly for invalid/corrupted PDFs
    - never execute embedded content
    """

    @property
    def supported_extensions(self) -> tuple[str, ...]:
        return (".pdf",)

    def parse(self, path: str | Path) -> ParsedDocument:
        path = Path(path)
        source_path = str(path.resolve())

        text_parts: list[str] = []
        metadata: dict = {
            "page_count": 0,
            "pages": [],
        }

        try:
            reader = PdfReader(str(path))
            page_count = len(reader.pages)
            metadata["page_count"] = page_count

            for i, page in enumerate(reader.pages, start=1):
                try:
                    page_text = page.extract_text()
                    page_text_stripped = page_text.strip() if page_text else ""
                    if page_text_stripped:
                        text_parts.append(page_text_stripped)
                    metadata["pages"].append(
                        {"page_number": i, "text_length": len(page_text_stripped)}
                    )
                except Exception as e:
                    logger.warning(f"Failed to extract text from page {i}: {e}")
                    metadata["pages"].append(
                        {"page_number": i, "text_length": 0, "error": str(e)}
                    )

            full_text = "\n".join(text_parts) if text_parts else ""

        except PdfReadError as e:
            logger.error(f"Corrupted PDF: {e}")
            full_text = ""
            metadata["page_count"] = 0
            metadata["pages"] = []
            # Do not raise - fail cleanly
        except Exception as e:
            logger.error(f"Unexpected error parsing PDF {path}: {e}")
            full_text = ""
            metadata["page_count"] = 0
            metadata["pages"] = []

        # Derive title from filename (stem) for PDFs
        title = path.stem if path.stem else "untitled"

        file_type = "application/pdf"

        return ParsedDocument(
            source_path=source_path,
            title=title,
            text=full_text,
            file_type=file_type,
            metadata=metadata,
        )