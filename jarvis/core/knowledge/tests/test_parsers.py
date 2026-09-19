"""Parser tests for JARVIS V4 document foundation."""

import tempfile
from pathlib import Path

import pytest

from core.knowledge.parsers.base import ParsedDocument, ParserBase
from core.knowledge.parsers.markdown import MarkdownParser
from core.knowledge.parsers.pdf import PdfParser
from core.knowledge.parsers.registry import ParserRegistry
from core.knowledge.parsers.text import TextParser


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def registry() -> ParserRegistry:
    return ParserRegistry()


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ── Text parser tests ───────────────────────────────────────────────────


def test_text_parser_basic(tmp_dir, caplog):
    """Basic text parsing with content (valid UTF-8)."""
    p = TextParser()
    file_path = tmp_dir / "hello.txt"
    file_path.write_text("Hello World\nThis is a test.", encoding="utf-8")
    with caplog.at_level("WARNING"):
        doc = p.parse(file_path)
    assert doc.text == "Hello World\nThis is a test."
    assert doc.title == "hello"
    assert doc.file_type == "text/plain"
    assert doc.metadata["encoding"] == "utf-8"
    assert doc.metadata["utf8_fallback"] is False


def test_text_parser_empty(tmp_dir):
    """Empty text file handled correctly."""
    p = TextParser()
    file_path = tmp_dir / "empty.txt"
    file_path.write_text("", encoding="utf-8")
    doc = p.parse(file_path)
    assert doc.text == ""
    assert doc.title == "empty"
    assert doc.metadata["encoding"] == "utf-8"
    assert doc.metadata["utf8_fallback"] is False
    assert doc.metadata["byte_count"] == 0


def test_text_parser_invalid_utf8_warning(tmp_dir, caplog):
    """Invalid UTF-8 triggers warning and exposes fallback status."""
    import warnings as _warnings
    p = TextParser()
    file_path = tmp_dir / "bad.txt"
    # Write bytes that are not valid UTF-8
    file_path.write_bytes(b"\xff\xfe invalid utf8 \n")
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        doc = p.parse(file_path)
    assert doc.text is not None
    assert doc.title == "bad"
    assert doc.metadata["encoding"] == "latin-1"
    assert doc.metadata["utf8_fallback"] is True
    # Warning should have been emitted
    assert any("not valid UTF-8" in str(warning.message) for warning in caught)


def test_text_parser_no_execute(tmp_dir):
    """Never execute file contents - test with potentially dangerous content."""
    p = TextParser()
    file_path = tmp_dir / "dangerous.txt"
    file_path.write_text("__import__(os)\nprint(1)", encoding="utf-8")
    doc = p.parse(file_path)
    # Text should be preserved as-is, not executed
    assert "__import__" in doc.text


# ── Markdown parser tests ───────────────────────────────────────────────


def test_markdown_parser_basic(tmp_dir):
    """Basic markdown parsing preserves content."""
    p = MarkdownParser()
    file_path = tmp_dir / "readme.md"
    file_path.write_text("Some **bold** and *italic* text.\n\n[Link](https://example.com)", encoding="utf-8")
    doc = p.parse(file_path)
    assert doc.title == "readme"  # No heading, so uses filename


def test_markdown_parser_heading_title(tmp_dir):
    """Title derived from first meaningful heading."""
    p = MarkdownParser()
    file_path = tmp_dir / "doc.md"
    file_path.write_text("# Main Title\n\nSome content here.", encoding="utf-8")
    doc = p.parse(file_path)
    assert doc.title == "Main Title"


def test_markdown_parser_no_heading_uses_filename(tmp_dir):
    """When no heading, use filename as title."""
    p = MarkdownParser()
    file_path = tmp_dir / "my_doc.md"
    file_path.write_text("Just some content without headings.", encoding="utf-8")
    doc = p.parse(file_path)
    assert doc.title == "my_doc"


def test_markdown_parser_preserves_hierarchy(tmp_dir):
    """Heading hierarchy preserved in metadata."""
    p = MarkdownParser()
    file_path = tmp_dir / "hierarchy.md"
    file_path.write_text("# H1\n## H2\n### H3\n\nContent", encoding="utf-8")
    doc = p.parse(file_path)
    assert doc.metadata["heading_count"] == 3
    assert doc.metadata["first_heading"] == "H1"


def test_markdown_parser_front_matter_detected(tmp_dir):
    """Front matter detected in metadata."""
    p = MarkdownParser()
    file_path = tmp_dir / "fm.md"
    file_path.write_text("---\ntitle: Test\n---\n\n# Real Title\n\nContent", encoding="utf-8")
    doc = p.parse(file_path)
    assert doc.metadata["has_front_matter"] is True


# ── PDF parser tests ────────────────────────────────────────────────────


def test_pdf_parser_basic(tmp_dir):
    """PDF parsing with pypdf - create a minimal PDF and parse it."""
    p = PdfParser()

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)

    # Write to a temp file - pypdf 6.x uses write() not save()
    pdf_path = tmp_dir / "sample.pdf"
    with open(str(pdf_path), "wb") as f:
        writer.write(f)

    doc = p.parse(pdf_path)
    assert doc.file_type == "application/pdf"
    assert doc.metadata["page_count"] == 2


def test_pdf_parser_page_metadata(tmp_dir):
    """Page numbers preserved in metadata."""
    p = PdfParser()

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)

    pdf_path = tmp_dir / "pages.pdf"
    with open(str(pdf_path), "wb") as f:
        writer.write(f)

    doc = p.parse(pdf_path)
    assert doc.metadata["page_count"] == 2
    assert len(doc.metadata["pages"]) == 2
    assert doc.metadata["pages"][0]["page_number"] == 1
    assert doc.metadata["pages"][1]["page_number"] == 2


def test_pdf_parser_corrupted(tmp_dir):
    """Corrupted PDF fails cleanly without raising."""
    p = PdfParser()

    # Write garbage bytes as a PDF file
    pdf_path = tmp_dir / "corrupted.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 corrupted garbage data here")

    doc = p.parse(pdf_path)
    assert doc.text == ""
    assert doc.metadata["page_count"] == 0


# ── Parser registry tests ───────────────────────────────────────────────


def test_registry_supported_extensions(registry):
    """Registry reports supported extensions."""
    exts = registry.supported_extensions()
    assert ".txt" in exts
    assert ".md" in exts
    assert ".pdf" in exts


def test_registry_can_parse(registry, tmp_dir):
    """Registry can check if a path is supported."""
    # Create a test txt file
    file_path = tmp_dir / "test.txt"
    file_path.write_text("test")

    assert registry.can_parse(file_path) is True
    assert registry.can_parse(str(file_path)) is True


def test_registry_cannot_parse(registry, tmp_dir):
    """Registry rejects unsupported extensions."""
    file_path = tmp_dir / "unknown.xyz"
    assert registry.can_parse(file_path) is False


def test_registry_get_parser(registry, tmp_dir):
    """Registry returns correct parser for a path."""
    file_path = tmp_dir / "test.txt"
    file_path.write_text("test")

    parser = registry.get_parser(file_path)
    assert isinstance(parser, TextParser)


def test_registry_get_parser_unsupported(registry, tmp_dir):
    """Registry raises ValueError for unsupported extension."""
    file_path = tmp_dir / "unknown.xyz"
    with pytest.raises(ValueError, match="Unsupported file extension"):
        registry.get_parser(file_path)


def test_registry_parse(registry, tmp_dir):
    """Registry parses file using matching parser."""
    file_path = tmp_dir / "test.txt"
    file_path.write_text("Hello World")

    doc = registry.parse(file_path)
    assert doc.text == "Hello World"
    assert doc.file_type == "text/plain"


# ── Edge cases ──────────────────────────────────────────────────────────


def test_parsers_are_instances_of_base():
    """All parser classes inherit from ParserBase."""
    from core.knowledge.parsers.text import TextParser
    from core.knowledge.parsers.markdown import MarkdownParser
    from core.knowledge.parsers.pdf import PdfParser

    for cls in [TextParser, MarkdownParser, PdfParser]:
        assert issubclass(cls, ParserBase)


def test_parsed_document_required_fields():
    """ParsedDocument has all required fields."""
    doc = ParsedDocument(
        source_path="/path/to/file.txt",
        title="Test",
        text="Content",
        file_type="text/plain",
    )
    assert doc.source_path == "/path/to/file.txt"
    assert doc.title == "Test"
    assert doc.text == "Content"
    assert doc.file_type == "text/plain"
    assert doc.metadata == {}


def test_empty_md_file(tmp_dir):
    """Empty markdown file handled correctly."""
    p = MarkdownParser()
    file_path = tmp_dir / "empty.md"
    file_path.write_text("", encoding="utf-8")
    doc = p.parse(file_path)
    assert doc.text == ""
    assert doc.title == "empty"


def test_unsupported_extension_error():
    """Clear error for unsupported extensions."""
    from core.knowledge.parsers.registry import ParserRegistry
    registry = ParserRegistry()
    with pytest.raises(ValueError, match="Unsupported file extension"):
        registry.get_parser("/path/to/file.docx")


def test_invalid_utf8_text(tmp_dir):
    """Invalid UTF-8 in text file handled cleanly."""
    p = TextParser()
    file_path = tmp_dir / "bad_utf8.txt"
    file_path.write_bytes(b"\x80\x81\x82 invalid bytes")
    doc = p.parse(file_path)
    assert doc.text is not None
    assert doc.title == "bad_utf8"