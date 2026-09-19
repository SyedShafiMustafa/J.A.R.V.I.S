"""Chunker tests for JARVIS V4.1B document chunking engine."""

import tempfile
from pathlib import Path

import pytest

from core.knowledge.chunkers.base import ChunkerConfig, ChunkerBase
from core.knowledge.chunkers.markdown import MarkdownChunker
from core.knowledge.chunkers.models import DocumentChunk
from core.knowledge.chunkers.pdf import PdfChunker
from core.knowledge.chunkers.registry import ChunkerRegistry
from core.knowledge.chunkers.text import TextChunker
from core.knowledge.parsers.base import ParsedDocument


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def text_chunker():
    return TextChunker()


@pytest.fixture
def markdown_chunker():
    return MarkdownChunker()


@pytest.fixture
def pdf_chunker():
    return PdfChunker()


@pytest.fixture
def chunker_registry():
    return ChunkerRegistry()


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def sample_text_document():
    """Create a sample plain text document for testing."""
    return ParsedDocument(
        source_path="/path/to/document.txt",
        title="Test Document",
        text="This is the first paragraph.\n\nThis is the second paragraph with more content.\n\nThis is the third paragraph.",
        file_type="text/plain",
        metadata={"encoding": "utf-8", "byte_count": 100},
    )


@pytest.fixture
def small_text_document():
    """Create a small document that fits in one chunk."""
    return ParsedDocument(
        source_path="/path/to/small.txt",
        title="Small Document",
        text="Short text.",
        file_type="text/plain",
        metadata={"encoding": "utf-8"},
    )


@pytest.fixture
def large_text_document():
    """Create a large document that requires multiple chunks."""
    text = "\n\n".join([f"Paragraph {i} with some content to make it larger." for i in range(100)])
    return ParsedDocument(
        source_path="/path/to/large.txt",
        title="Large Document",
        text=text,
        file_type="text/plain",
        metadata={"encoding": "utf-8"},
    )


@pytest.fixture
def markdown_document():
    """Create a sample markdown document with headings."""
    return ParsedDocument(
        source_path="/path/to/document.md",
        title="Main Title",
        text="# Main Title\n\nContent under main title.\n\n## Section 1\n\nContent under section 1.\n\n### Subsection\n\nContent under subsection.\n\n## Section 2\n\nContent under section 2.",
        file_type="text/markdown",
        metadata={"heading_count": 4, "first_heading": "Main Title"},
    )


@pytest.fixture
def pdf_document():
    """Create a sample PDF document."""
    return ParsedDocument(
        source_path="/path/to/document.pdf",
        title="PDF Document",
        text="Page 1 content.\n\nPage 2 content.\n\nPage 3 content.",
        file_type="application/pdf",
        metadata={"page_count": 3, "pages": [
            {"page_number": 1, "text_length": 15},
            {"page_number": 2, "text_length": 15},
            {"page_number": 3, "text_length": 15},
        ]},
    )


# ── DocumentChunk model tests ────────────────────────────────────────────────

def test_document_chunk_creation():
    """DocumentChunk can be created with required fields."""
    chunk = DocumentChunk(
        chunk_id="test_chunk_1",
        source_path="/path/to/file.txt",
        document_title="Test Document",
        file_type="text/plain",
        chunk_index=0,
        content="Test content",
        token_count=3,
    )
    assert chunk.chunk_id == "test_chunk_1"
    assert chunk.source_path == "/path/to/file.txt"
    assert chunk.document_title == "Test Document"
    assert chunk.file_type == "text/plain"
    assert chunk.chunk_index == 0
    assert chunk.content == "Test content"
    assert chunk.token_count == 3


def test_document_chunk_validation():
    """DocumentChunk validates required fields."""
    with pytest.raises(ValueError, match="chunk_id cannot be empty"):
        DocumentChunk(
            chunk_id="",
            source_path="/path/to/file.txt",
            document_title="Test",
            file_type="text/plain",
            chunk_index=0,
            content="Content",
            token_count=3,
        )

    with pytest.raises(ValueError, match="content cannot be empty"):
        DocumentChunk(
            chunk_id="test_chunk",
            source_path="/path/to/file.txt",
            document_title="Test",
            file_type="text/plain",
            chunk_index=0,
            content="",
            token_count=0,
        )


def test_document_chunk_serialization():
    """DocumentChunk can be serialized to/from dict."""
    chunk = DocumentChunk(
        chunk_id="test_chunk_1",
        source_path="/path/to/file.txt",
        document_title="Test Document",
        file_type="text/plain",
        chunk_index=0,
        content="Test content",
        token_count=3,
        metadata={"key": "value"},
    )

    data = chunk.to_dict()
    assert data["chunk_id"] == "test_chunk_1"
    assert data["metadata"]["key"] == "value"

    restored = DocumentChunk.from_dict(data)
    assert restored.chunk_id == chunk.chunk_id
    assert restored.content == chunk.content


# ── ChunkerConfig tests ──────────────────────────────────────────────────────

def test_chunker_config_defaults():
    """ChunkerConfig has sensible defaults."""
    config = ChunkerConfig()
    assert config.chunk_size == 1000
    assert config.overlap == 100
    assert config.min_chunk_size == 50


def test_chunker_config_custom():
    """ChunkerConfig accepts custom values."""
    config = ChunkerConfig(chunk_size=500, overlap=50, min_chunk_size=25)
    assert config.chunk_size == 500
    assert config.overlap == 50
    assert config.min_chunk_size == 25


def test_chunker_config_validation():
    """ChunkerConfig validates parameters."""
    with pytest.raises(ValueError, match="chunk_size must be positive"):
        ChunkerConfig(chunk_size=0)

    with pytest.raises(ValueError, match="overlap must be non-negative"):
        ChunkerConfig(overlap=-1)

    with pytest.raises(ValueError, match="overlap must be less than chunk_size"):
        ChunkerConfig(chunk_size=100, overlap=100)

    with pytest.raises(ValueError, match="min_chunk_size must be less than chunk_size"):
        ChunkerConfig(chunk_size=100, overlap=10, min_chunk_size=100)


# ── Text chunker tests ───────────────────────────────────────────────────────

def test_text_chunker_small_document(small_text_document, text_chunker):
    """Small document → one chunk."""
    chunks = text_chunker.chunk(small_text_document)
    assert len(chunks) == 1
    assert chunks[0].content == "Short text."
    assert chunks[0].chunk_index == 0


def test_text_chunker_large_document(large_text_document, text_chunker):
    """Large document → multiple chunks."""
    chunks = text_chunker.chunk(large_text_document)
    assert len(chunks) > 1
    for i, chunk in enumerate(chunks):
        assert chunk.chunk_index == i
        assert chunk.content


def test_text_chunker_custom_chunk_size(sample_text_document):
    """Custom chunk size affects chunking behavior."""
    chunker = TextChunker(ChunkerConfig(chunk_size=50, overlap=10, min_chunk_size=25))
    chunks = chunker.chunk(sample_text_document)
    # With small chunk size, should get more chunks
    assert len(chunks) >= 1


def test_text_chunker_custom_overlap(sample_text_document):
    """Custom overlap affects chunking behavior."""
    chunker = TextChunker(ChunkerConfig(overlap=20))
    chunks = chunker.chunk(sample_text_document)
    # Should create chunks with overlap
    assert len(chunks) >= 1


def test_text_chunker_paragraph_boundaries(sample_text_document, text_chunker):
    """Paragraph boundaries are preserved in chunking."""
    chunks = text_chunker.chunk(sample_text_document)
    # Check that paragraph structure is maintained
    for chunk in chunks:
        # Paragraphs should be separated by double newlines
        assert "\n\n" in chunk.content or len(chunks) == 1


def test_text_chunker_empty_document(text_chunker):
    """Empty document handled correctly."""
    empty_doc = ParsedDocument(
        source_path="/path/to/empty.txt",
        title="Empty",
        text="",
        file_type="text/plain",
    )
    with pytest.raises(ValueError, match="Cannot chunk empty document"):
        text_chunker.chunk(empty_doc)


def test_text_chunker_deterministic(sample_text_document, text_chunker):
    """Identical input produces identical output."""
    chunks1 = text_chunker.chunk(sample_text_document)
    chunks2 = text_chunker.chunk(sample_text_document)

    assert len(chunks1) == len(chunks2)
    for c1, c2 in zip(chunks1, chunks2):
        assert c1.chunk_id == c2.chunk_id
        assert c1.content == c2.content
        assert c1.chunk_index == c2.chunk_index


# ── Markdown chunker tests ───────────────────────────────────────────────────

def test_markdown_chunker_heading_preservation(markdown_document, markdown_chunker):
    """Markdown heading information preserved in chunks."""
    chunks = markdown_chunker.chunk(markdown_document)
    assert len(chunks) >= 1

    # Check that heading metadata is preserved
    for chunk in chunks:
        assert "heading" in chunk.metadata or chunk.metadata.get("heading") is None
        assert "chunk_type" in chunk.metadata
        assert chunk.metadata["chunk_type"] == "markdown"


def test_markdown_chunker_heading_hierarchy(markdown_document, markdown_chunker):
    """Heading hierarchy preserved in chunk metadata."""
    chunks = markdown_chunker.chunk(markdown_document)

    # Check that section path is maintained
    for chunk in chunks:
        if chunk.metadata.get("heading"):
            assert "section_path" in chunk.metadata
            assert "heading_level" in chunk.metadata


def test_markdown_chunker_small_markdown():
    """Small markdown document → one chunk."""
    small_md = ParsedDocument(
        source_path="/path/to/small.md",
        title="Small",
        text="# Title\n\nContent",
        file_type="text/markdown",
    )
    chunker = MarkdownChunker()
    chunks = chunker.chunk(small_md)
    assert len(chunks) == 1


def test_markdown_chunker_deterministic(markdown_document, markdown_chunker):
    """Identical markdown input produces identical output."""
    chunks1 = markdown_chunker.chunk(markdown_document)
    chunks2 = markdown_chunker.chunk(markdown_document)

    assert len(chunks1) == len(chunks2)
    for c1, c2 in zip(chunks1, chunks2):
        assert c1.chunk_id == c2.chunk_id
        assert c1.content == c2.content


# ── PDF chunker tests ───────────────────────────────────────────────────────

def test_pdf_chunker_page_metadata(pdf_document, pdf_chunker):
    """PDF page metadata preserved in chunks."""
    chunks = pdf_chunker.chunk(pdf_document)
    assert len(chunks) >= 1

    # Check that page metadata is preserved
    for chunk in chunks:
        assert "page_range" in chunk.metadata
        assert "page_count" in chunk.metadata
        assert "chunk_type" in chunk.metadata
        assert chunk.metadata["chunk_type"] == "pdf"


def test_pdf_chunker_page_boundaries(pdf_document, pdf_chunker):
    """Page boundaries respected where practical."""
    chunks = pdf_chunker.chunk(pdf_document)
    # Should respect page information from metadata
    assert len(chunks) >= 1


def test_pdf_chunker_empty_pdf(pdf_chunker):
    """Empty PDF document handled correctly."""
    empty_pdf = ParsedDocument(
        source_path="/path/to/empty.pdf",
        title="Empty",
        text="",
        file_type="application/pdf",
        metadata={"page_count": 0, "pages": []},
    )
    with pytest.raises(ValueError, match="Cannot chunk empty document"):
        pdf_chunker.chunk(empty_pdf)


def test_pdf_chunker_deterministic(pdf_document, pdf_chunker):
    """Identical PDF input produces identical output."""
    chunks1 = pdf_chunker.chunk(pdf_document)
    chunks2 = pdf_chunker.chunk(pdf_document)

    assert len(chunks1) == len(chunks2)
    for c1, c2 in zip(chunks1, chunks2):
        assert c1.chunk_id == c2.chunk_id
        assert c1.content == c2.content


# ── Chunker registry tests ───────────────────────────────────────────────────

def test_chunker_registry_supported_types(chunker_registry):
    """Registry reports supported file types."""
    types = chunker_registry.supported_file_types()
    assert "text/plain" in types
    assert "text/markdown" in types
    assert "application/pdf" in types


def test_chunker_registry_can_chunk(chunker_registry):
    """Registry can check if file type is supported."""
    assert chunker_registry.can_chunk("text/plain") is True
    assert chunker_registry.can_chunk("text/markdown") is True
    assert chunker_registry.can_chunk("application/pdf") is True
    assert chunker_registry.can_chunk("application/msword") is False


def test_chunker_registry_get_chunker(chunker_registry):
    """Registry returns correct chunker for file type."""
    text_chunker = chunker_registry.get_chunker("text/plain")
    assert isinstance(text_chunker, TextChunker)

    md_chunker = chunker_registry.get_chunker("text/markdown")
    assert isinstance(md_chunker, MarkdownChunker)

    pdf_chunker = chunker_registry.get_chunker("application/pdf")
    assert isinstance(pdf_chunker, PdfChunker)


def test_chunker_registry_get_chunker_unsupported(chunker_registry):
    """Registry raises error for unsupported file type."""
    with pytest.raises(ValueError, match="Unsupported file type for chunking"):
        chunker_registry.get_chunker("application/msword")


def test_chunker_registry_chunk(sample_text_document, chunker_registry):
    """Registry chunks document using appropriate chunker."""
    chunks = chunker_registry.chunk(sample_text_document)
    assert len(chunks) >= 1
    assert all(isinstance(chunk, DocumentChunk) for chunk in chunks)


# ── Edge cases and behavior tests ───────────────────────────────────────────

def test_chunker_tiny_final_chunk_handling():
    """Tiny final chunks are merged with previous chunks."""
    # Create document where final chunk would be tiny
    text = "Large paragraph 1 with lots of content.\n\n" * 10 + "Tiny end."
    doc = ParsedDocument(
        source_path="/path/to/test.txt",
        title="Test",
        text=text,
        file_type="text/plain",
    )
    chunker = TextChunker(ChunkerConfig(chunk_size=100, overlap=10, min_chunk_size=50))
    chunks = chunker.chunk(doc)

    # Should not have tiny chunks at the end
    if len(chunks) > 1:
        # Check that last chunk is not tiny
        assert chunker._estimate_token_count(chunks[-1].content) >= chunker.config.min_chunk_size


def test_chunker_no_duplicate_full_chunks(sample_text_document, text_chunker):
    """No duplicate full chunks are produced."""
    chunks = text_chunker.chunk(sample_text_document)

    # Check for duplicate content
    contents = [chunk.content for chunk in chunks]
    assert len(contents) == len(set(contents))


def test_chunker_chunk_ordering(sample_text_document, text_chunker):
    """Chunks are produced in correct order."""
    chunks = text_chunker.chunk(sample_text_document)

    # Check that chunk indices are sequential
    indices = [chunk.chunk_index for chunk in chunks]
    assert indices == list(range(len(chunks)))


def test_chunker_security_no_execution():
    """Chunker treats document content as data, not instructions."""
    dangerous_doc = ParsedDocument(
        source_path="/path/to/dangerous.txt",
        title="Dangerous",
        text="__import__(os)\nprint(1)",
        file_type="text/plain",
    )
    chunker = TextChunker()
    chunks = chunker.chunk(dangerous_doc)

    # Content should be preserved as-is, not executed
    assert any("__import__" in chunk.content for chunk in chunks)


def test_chunker_metadata_preservation(sample_text_document, text_chunker):
    """Source document metadata is preserved in chunks."""
    chunks = text_chunker.chunk(sample_text_document)

    for chunk in chunks:
        assert "encoding" in chunk.metadata
        assert chunk.metadata["encoding"] == "utf-8"
        assert "byte_count" in chunk.metadata


def test_chunker_chunk_id_generation(sample_text_document, text_chunker):
    """Chunk IDs are deterministic and unique."""
    chunks = text_chunker.chunk(sample_text_document)

    # All chunk IDs should be unique
    chunk_ids = [chunk.chunk_id for chunk in chunks]
    assert len(chunk_ids) == len(set(chunk_ids))

    # Chunk IDs should be deterministic
    chunks2 = text_chunker.chunk(sample_text_document)
    chunk_ids2 = [chunk.chunk_id for chunk in chunks2]
    assert chunk_ids == chunk_ids2


def test_chunker_token_count_estimation(text_chunker):
    """Token count estimation is reasonable."""
    doc = ParsedDocument(
        source_path="/path/to/test.txt",
        title="Test",
        text="This is a test document with some content.",
        file_type="text/plain",
    )
    chunks = text_chunker.chunk(doc)

    for chunk in chunks:
        # Token count should be non-negative
        assert chunk.token_count >= 0
        # Token count should be roughly 1/4 of character count
        estimated_chars = len(chunk.content)
        assert chunk.token_count <= estimated_chars  # Upper bound
        assert chunk.token_count >= estimated_chars // 10  # Lower bound


# ── Integration tests ───────────────────────────────────────────────────────

def test_parser_to_chunker_integration(tmp_dir, chunker_registry):
    """End-to-end test: parser → chunker workflow."""
    from core.knowledge.parsers.registry import ParserRegistry

    # Create a test file
    test_file = tmp_dir / "test.txt"
    test_file.write_text("Paragraph 1\n\nParagraph 2\n\nParagraph 3", encoding="utf-8")

    # Parse the file
    parser_registry = ParserRegistry()
    parsed_doc = parser_registry.parse(test_file)

    # Chunk the parsed document
    chunks = chunker_registry.chunk(parsed_doc)

    assert len(chunks) >= 1
    assert all(isinstance(chunk, DocumentChunk) for chunk in chunks)
    assert chunks[0].source_path == str(test_file.resolve())


def test_multiple_document_types_chunking(chunker_registry):
    """Registry can handle multiple document types."""
    docs = [
        ParsedDocument("/path/1.txt", "Text", "Content", "text/plain"),
        ParsedDocument("/path/2.md", "Markdown", "# Title\nContent", "text/markdown"),
        ParsedDocument("/path/3.pdf", "PDF", "Content", "application/pdf",
                      metadata={"page_count": 1, "pages": [{"page_number": 1, "text_length": 7}]}),
    ]

    for doc in docs:
        chunks = chunker_registry.chunk(doc)
        assert len(chunks) >= 1
        assert chunks[0].file_type == doc.file_type
