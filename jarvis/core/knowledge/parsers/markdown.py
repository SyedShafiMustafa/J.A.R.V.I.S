from __future__ import annotations

import re
from pathlib import Path

from .base import ParserBase, ParsedDocument


class MarkdownParser(ParserBase):
    """Parser for .md files.

    - preserve meaningful Markdown text
    - detect headings
    - preserve heading hierarchy in metadata
    - derive title from first meaningful heading when present
    - otherwise use filename
    - treat content as data, never instructions
    """

    @property
    def supported_extensions(self) -> tuple[str, ...]:
        return (".md",)

    def _find_first_heading(self, text: str) -> str | None:
        """Find the first heading (# ## ###) in the text."""
        for line in text.splitlines():
            stripped = line.strip()
            if re.match(r"^#{1,6}\s+", stripped):
                # Strip the hashes and get the heading text
                heading_text = re.sub(r"^#{1,6}\s+", "", stripped).strip()
                if heading_text:
                    return heading_text
        return None

    def _strip_markdown_syntax(self, text: str) -> str:
        """Remove inline Markdown syntax while preserving content text.

        Keeps headings, bold, italic, links, code as plain text.
        """
        # Remove code blocks (fenced)
        text = re.sub(r"```[\s\S]*?```", " ", text)
        # Remove inline code
        text = re.sub(r"`([^`]+)`", r"\1", text)
        # Remove bold **text** or __text__
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"__([^_]+)__", r"\1", text)
        # Remove italic *text* or _text_
        text = re.sub(r"\*([^*]+)\*", r"\1", text)
        text = re.sub(r"^([_]+)(.+)\1$", r"\2", text, flags=re.MULTILINE)
        # Remove links [text](url) -> text
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        # Remove reference-style labels [text]: url
        text = re.sub(r"^\s*\[[^\]]*\]:\s*$", "", text, flags=re.MULTILINE)
        # Remove excess whitespace
        text = re.sub(r"\n\s*\n", "\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    def parse(self, path: str | Path) -> ParsedDocument:
        path = Path(path)
        source_path = str(path.resolve())

        # Read the file as UTF-8
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="latin-1") as f:
                text = f.read()

        # Derive title from first meaningful heading, or filename
        title = self._find_first_heading(text)
        if not title:
            title = path.stem if path.stem else "untitled"

        # Strip Markdown syntax to get clean text content
        clean_text = self._strip_markdown_syntax(text)

        file_type = "text/markdown"

        metadata = {
            "has_front_matter": bool(re.match(r"^---[\s\S]*?---", text, re.MULTILINE)),
            "heading_count": len(re.findall(r"^#{1,6}\s+", text, re.MULTILINE)),
            "first_heading": title,
        }

        return ParsedDocument(
            source_path=source_path,
            title=title,
            text=clean_text,
            file_type=file_type,
            metadata=metadata,
        )