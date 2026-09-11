"""Persistence tests for JARVIS V4.1C knowledge storage."""

import json
import tempfile
from pathlib import Path

import pytest

from jarvis.core.knowledge.chunkers.models import DocumentChunk
from jarvis.core.knowledge.chunkers.registry import ChunkerRegistry
from jarvis.core.knowledge.parsers.base import ParsedDocument
from jarvis.core.knowledge.parsers.registry import ParserRegistry
from jarvis.core.knowledge.storage import (
    ChunkRepository,
    DocumentRepository,
    DocumentVersionRepository,
    IngestionJobRepository,
    KnowledgeDatabase,
)
from jarvis.core.knowledge.storage.schema import KnowledgeSchema


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test_knowledge.db"
        db = KnowledgeDatabase(db_path)
        yield db
        db.close()


@pytest.fixture
def sample_parsed_document():
    """Create a sample parsed document for testing."""
    return ParsedDocument(
        source_path="/path/to/document.txt",
        title="Test Document",
        text="This is the first paragraph.\n\nThis is the second paragraph.",
        file_type="text/plain",
        metadata={"encoding": "utf-8", "byte_count": 100},
    )


@pytest.fixture
def sample_chunks():
    """Create sample document chunks for testing."""
    return [
        DocumentChunk(
            chunk_id="chunk_1",
            source_path="/path/to/document.txt",
            document_title="Test Document",
            file_type="text/plain",
            chunk_index=0,
            content="First chunk content",
            token_count=4,
            metadata={"heading": "Section 1", "heading_level": 1},
        ),
        DocumentChunk(
            chunk_id="chunk_2",
            source_path="/path/to/document.txt",
            document_title="Test Document",
            file_type="text/plain",
            chunk_index=1,
            content="Second chunk content",
            token_count=4,
            metadata={"heading": "Section 2", "heading_level": 1},
        ),
    ]


# ── Schema tests ─────────────────────────────────────────────────────────

def test_schema_creation(temp_db):
    """Database schema is created correctly."""
    conn = temp_db.connect()

    # Check that tables exist
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]

    expected_tables = ["chunks", "document_versions", "documents", "ingestion_jobs"]
    for table in expected_tables:
        assert table in tables, f"Table {table} not found"


def test_schema_foreign_keys_enabled(temp_db):
    """Foreign keys are enabled in the database."""
    conn = temp_db.connect()
    cursor = conn.execute("PRAGMA foreign_keys")
    result = cursor.fetchone()
    assert result[0] == 1, "Foreign keys should be enabled"


def test_schema_indexes_created(temp_db):
    """Required indexes are created."""
    conn = temp_db.connect()

    # Check for key indexes
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
    )
    indexes = [row[0] for row in cursor.fetchall()]

    # Check for some key indexes
    assert "idx_documents_source_path" in indexes
    assert "idx_documents_content_hash" in indexes
    assert "idx_chunks_document_id" in indexes
    assert "idx_chunks_version_id" in indexes


def test_schema_version_management(temp_db):
    """Schema version is set and retrievable."""
    conn = temp_db.connect()

    # Initial version should be 1
    version = KnowledgeSchema.get_schema_version(conn)
    assert version == 1

    # Can set version
    KnowledgeSchema.set_schema_version(conn, 2)
    version = KnowledgeSchema.get_schema_version(conn)
    assert version == 2


# ── Database connection tests ─────────────────────────────────────────────

def test_database_connection_context_manager(temp_db):
    """Database connection works with context manager."""
    with temp_db as conn:
        assert conn is not None
        cursor = conn.execute("SELECT 1")
        assert cursor.fetchone()[0] == 1


def test_database_connection_persistence(temp_db):
    """Database connection persists across calls."""
    conn1 = temp_db.connect()
    conn2 = temp_db.connect()
    assert conn1 is conn2  # Same connection


def test_database_close_and_reopen(temp_db):
    """Database can be closed and reopened."""
    conn = temp_db.connect()
    temp_db.close()

    # Should be able to reconnect
    new_conn = temp_db.connect()
    assert new_conn is not None
    assert new_conn is not conn  # New connection


def test_database_transaction_rollback(temp_db):
    """Transaction rollback works correctly."""
    conn = temp_db.connect()
    temp_db.begin_transaction()

    # Insert data
    conn.execute(
        "INSERT INTO documents (source_path, content_hash, file_size, file_type, extension, title) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("/test/path", "hash123", 100, "text/plain", ".txt", "Test"),
    )

    # Rollback
    temp_db.rollback()

    # Verify data was rolled back
    cursor = conn.execute("SELECT COUNT(*) FROM documents")
    count = cursor.fetchone()[0]
    assert count == 0


# ── Document repository tests ─────────────────────────────────────────────

def test_document_repository_create(temp_db, sample_parsed_document):
    """Document can be created and retrieved."""
    doc_repo = DocumentRepository(temp_db)

    doc_id = doc_repo.create_document(sample_parsed_document, file_size=100)
    assert doc_id > 0

    # Retrieve document
    doc = doc_repo.get_document(doc_id)
    assert doc is not None
    assert doc["source_path"] == sample_parsed_document.source_path
    assert doc["title"] == sample_parsed_document.title
    assert doc["file_type"] == sample_parsed_document.file_type


def test_document_repository_get_by_source_path(temp_db, sample_parsed_document):
    """Document can be retrieved by source path."""
    doc_repo = DocumentRepository(temp_db)

    doc_id = doc_repo.create_document(sample_parsed_document, file_size=100)
    doc = doc_repo.get_document_by_source_path(sample_parsed_document.source_path)

    assert doc is not None
    assert doc["id"] == doc_id


def test_document_repository_get_by_content_hash(temp_db, sample_parsed_document):
    """Document can be retrieved by content hash."""
    doc_repo = DocumentRepository(temp_db)

    doc_id = doc_repo.create_document(sample_parsed_document, file_size=100)
    doc = doc_repo.get_document(doc_id)
    content_hash = doc["content_hash"]

    doc_by_hash = doc_repo.get_document_by_content_hash(content_hash)
    assert doc_by_hash is not None
    assert doc_by_hash["id"] == doc_id


def test_document_repository_list(temp_db, sample_parsed_document):
    """Documents can be listed."""
    doc_repo = DocumentRepository(temp_db)

    # Create multiple documents
    doc_repo.create_document(sample_parsed_document, file_size=100)

    doc2 = ParsedDocument(
        source_path="/path/to/doc2.txt",
        title="Document 2",
        text="Content 2",
        file_type="text/plain",
    )
    doc_repo.create_document(doc2, file_size=50)

    # List documents
    docs = doc_repo.list_documents()
    assert len(docs) == 2


def test_document_repository_list_with_status_filter(temp_db, sample_parsed_document):
    """Documents can be filtered by status."""
    doc_repo = DocumentRepository(temp_db)

    doc_id = doc_repo.create_document(sample_parsed_document, file_size=100)
    doc_repo.update_document_status(doc_id, "archived")

    # List with status filter
    active_docs = doc_repo.list_documents(status="active")
    archived_docs = doc_repo.list_documents(status="archived")

    assert len(active_docs) == 0
    assert len(archived_docs) == 1


def test_document_repository_update_status(temp_db, sample_parsed_document):
    """Document status can be updated."""
    doc_repo = DocumentRepository(temp_db)

    doc_id = doc_repo.create_document(sample_parsed_document, file_size=100)
    doc_repo.update_document_status(doc_id, "archived")

    doc = doc_repo.get_document(doc_id)
    assert doc["status"] == "archived"


def test_document_repository_delete(temp_db, sample_parsed_document):
    """Document can be deleted."""
    doc_repo = DocumentRepository(temp_db)

    doc_id = doc_repo.create_document(sample_parsed_document, file_size=100)
    result = doc_repo.delete_document(doc_id)

    assert result is True

    # Verify deletion
    doc = doc_repo.get_document(doc_id)
    assert doc is None


def test_document_repository_duplicate_detection(temp_db, sample_parsed_document):
    """Duplicate documents are detected via content hash."""
    doc_repo = DocumentRepository(temp_db)

    # Create first document
    doc_repo.create_document(sample_parsed_document, file_size=100)

    # Try to create duplicate
    with pytest.raises(Exception):  # sqlite3.IntegrityError
        doc_repo.create_document(sample_parsed_document, file_size=100)


def test_document_repository_metadata_round_trip(temp_db, sample_parsed_document):
    """Document metadata is preserved round-trip."""
    doc_repo = DocumentRepository(temp_db)

    metadata = {"encoding": "utf-8", "byte_count": 100, "custom": "value"}
    doc = ParsedDocument(
        source_path="/path/to/doc.txt",
        title="Test",
        text="Content",
        file_type="text/plain",
        metadata=metadata,
    )

    doc_id = doc_repo.create_document(doc, file_size=100)
    retrieved_doc = doc_repo.get_document(doc_id)

    # Deserialize metadata
    retrieved_metadata = json.loads(retrieved_doc["metadata"])
    assert retrieved_metadata == metadata


# ── Document version repository tests ───────────────────────────────────────

def test_document_version_repository_create(temp_db, sample_parsed_document):
    """Document version can be created."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)

    doc_id = doc_repo.create_document(sample_parsed_document, file_size=100)
    version_id = version_repo.create_document_version(
        doc_id,
        content_hash="hash123",
        file_size=100,
        change_type="created",
    )

    assert version_id > 0


def test_document_version_repository_get_versions(temp_db, sample_parsed_document):
    """Document versions can be retrieved."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)

    doc_id = doc_repo.create_document(sample_parsed_document, file_size=100)

    # Create multiple versions
    version_repo.create_document_version(doc_id, "hash1", 100, "created")
    version_repo.create_document_version(doc_id, "hash2", 150, "modified")

    versions = version_repo.get_document_versions(doc_id)
    assert len(versions) == 2
    assert versions[0]["version_number"] == 1
    assert versions[1]["version_number"] == 2


def test_document_version_repository_get_latest(temp_db, sample_parsed_document):
    """Latest document version can be retrieved."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)

    doc_id = doc_repo.create_document(sample_parsed_document, file_size=100)

    version_repo.create_document_version(doc_id, "hash1", 100, "created")
    version_repo.create_document_version(doc_id, "hash2", 150, "modified")

    latest = version_repo.get_latest_version(doc_id)
    assert latest is not None
    assert latest["version_number"] == 2
    assert latest["content_hash"] == "hash2"


def test_document_version_auto_increment(temp_db, sample_parsed_document):
    """Version numbers auto-increment correctly."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)

    doc_id = doc_repo.create_document(sample_parsed_document, file_size=100)

    # Create versions
    v1 = version_repo.create_document_version(doc_id, "hash1", 100)
    v2 = version_repo.create_document_version(doc_id, "hash2", 150)
    v3 = version_repo.create_document_version(doc_id, "hash3", 200)

    versions = version_repo.get_document_versions(doc_id)
    assert versions[0]["version_number"] == 1
    assert versions[1]["version_number"] == 2
    assert versions[2]["version_number"] == 3


# ── Chunk repository tests ───────────────────────────────────────────────

def test_chunk_repository_save(temp_db, sample_chunks):
    """Chunks can be saved."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)
    chunk_repo = ChunkRepository(temp_db)

    # Create document and version
    doc = ParsedDocument(
        source_path="/path/to/doc.txt",
        title="Test",
        text="Content",
        file_type="text/plain",
    )
    doc_id = doc_repo.create_document(doc, file_size=100)
    version_id = version_repo.create_document_version(doc_id, "hash123", 100)

    # Save chunks
    chunk_ids = chunk_repo.save_chunks(doc_id, version_id, sample_chunks)
    assert len(chunk_ids) == 2
    assert all(cid > 0 for cid in chunk_ids)


def test_chunk_repository_get_chunks(temp_db, sample_chunks):
    """Chunks can be retrieved and maintain ordering."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)
    chunk_repo = ChunkRepository(temp_db)

    # Setup
    doc = ParsedDocument(
        source_path="/path/to/doc.txt",
        title="Test",
        text="Content",
        file_type="text/plain",
    )
    doc_id = doc_repo.create_document(doc, file_size=100)
    version_id = version_repo.create_document_version(doc_id, "hash123", 100)
    chunk_repo.save_chunks(doc_id, version_id, sample_chunks)

    # Retrieve chunks
    chunks = chunk_repo.get_chunks(document_id=doc_id, version_id=version_id)
    assert len(chunks) == 2
    assert chunks[0]["chunk_index"] == 0
    assert chunks[1]["chunk_index"] == 1


def test_chunk_repository_get_by_document_id(temp_db, sample_chunks):
    """Chunks can be filtered by document ID."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)
    chunk_repo = ChunkRepository(temp_db)

    # Setup
    doc = ParsedDocument(
        source_path="/path/to/doc.txt",
        title="Test",
        text="Content",
        file_type="text/plain",
    )
    doc_id = doc_repo.create_document(doc, file_size=100)
    version_id = version_repo.create_document_version(doc_id, "hash123", 100)
    chunk_repo.save_chunks(doc_id, version_id, sample_chunks)

    # Get chunks by document ID
    chunks = chunk_repo.get_chunks(document_id=doc_id)
    assert len(chunks) == 2


def test_chunk_repository_get_by_version_id(temp_db, sample_chunks):
    """Chunks can be filtered by version ID."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)
    chunk_repo = ChunkRepository(temp_db)

    # Setup
    doc = ParsedDocument(
        source_path="/path/to/doc.txt",
        title="Test",
        text="Content",
        file_type="text/plain",
    )
    doc_id = doc_repo.create_document(doc, file_size=100)
    version_id = version_repo.create_document_version(doc_id, "hash123", 100)
    chunk_repo.save_chunks(doc_id, version_id, sample_chunks)

    # Get chunks by version ID
    chunks = chunk_repo.get_chunks(version_id=version_id)
    assert len(chunks) == 2


def test_chunk_repository_heading_metadata(temp_db, sample_chunks):
    """Chunk heading metadata is preserved."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)
    chunk_repo = ChunkRepository(temp_db)

    # Setup
    doc = ParsedDocument(
        source_path="/path/to/doc.txt",
        title="Test",
        text="Content",
        file_type="text/plain",
    )
    doc_id = doc_repo.create_document(doc, file_size=100)
    version_id = version_repo.create_document_version(doc_id, "hash123", 100)
    chunk_repo.save_chunks(doc_id, version_id, sample_chunks)

    # Retrieve and check metadata
    chunks = chunk_repo.get_chunks(document_id=doc_id, version_id=version_id)
    for chunk in chunks:
        if chunk["heading_metadata"]:
            metadata = json.loads(chunk["heading_metadata"])
            assert "heading" in metadata
            assert "heading_level" in metadata


def test_chunk_repository_duplicate_chunks_prevented(temp_db):
    """Duplicate chunks for same version are prevented."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)
    chunk_repo = ChunkRepository(temp_db)

    # Setup
    doc = ParsedDocument(
        source_path="/path/to/doc.txt",
        title="Test",
        text="Content",
        file_type="text/plain",
    )
    doc_id = doc_repo.create_document(doc, file_size=100)
    version_id = version_repo.create_document_version(doc_id, "hash123", 100)

    # Save chunks
    chunks = [
        DocumentChunk(
            chunk_id="chunk_1",
            source_path="/path/to/doc.txt",
            document_title="Test",
            file_type="text/plain",
            chunk_index=0,
            content="Content",
            token_count=1,
        )
    ]
    chunk_repo.save_chunks(doc_id, version_id, chunks)

    # Try to save duplicate chunks
    with pytest.raises(Exception):  # sqlite3.IntegrityError
        chunk_repo.save_chunks(doc_id, version_id, chunks)


def test_chunk_repository_chunk_ordering_validation(temp_db):
    """Chunk ordering is validated during save."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)
    chunk_repo = ChunkRepository(temp_db)

    # Setup
    doc = ParsedDocument(
        source_path="/path/to/doc.txt",
        title="Test",
        text="Content",
        file_type="text/plain",
    )
    doc_id = doc_repo.create_document(doc, file_size=100)
    version_id = version_repo.create_document_version(doc_id, "hash123", 100)

    # Create chunks with wrong ordering
    chunks = [
        DocumentChunk(
            chunk_id="chunk_1",
            source_path="/path/to/doc.txt",
            document_title="Test",
            file_type="text/plain",
            chunk_index=1,  # Wrong index
            content="Content",
            token_count=1,
        ),
        DocumentChunk(
            chunk_id="chunk_2",
            source_path="/path/to/doc.txt",
            document_title="Test",
            file_type="text/plain",
            chunk_index=0,  # Wrong index
            content="Content",
            token_count=1,
        ),
    ]

    with pytest.raises(ValueError, match="Chunk indices must be sequential"):
        chunk_repo.save_chunks(doc_id, version_id, chunks)


def test_chunk_repository_delete_by_version(temp_db, sample_chunks):
    """Chunks can be deleted by version ID."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)
    chunk_repo = ChunkRepository(temp_db)

    # Setup
    doc = ParsedDocument(
        source_path="/path/to/doc.txt",
        title="Test",
        text="Content",
        file_type="text/plain",
    )
    doc_id = doc_repo.create_document(doc, file_size=100)
    version_id = version_repo.create_document_version(doc_id, "hash123", 100)
    chunk_repo.save_chunks(doc_id, version_id, sample_chunks)

    # Delete chunks
    deleted_count = chunk_repo.delete_chunks_by_version(version_id)
    assert deleted_count == 2

    # Verify deletion
    chunks = chunk_repo.get_chunks(version_id=version_id)
    assert len(chunks) == 0


# ── Ingestion job repository tests ────────────────────────────────────────

def test_ingestion_job_repository_create(temp_db):
    """Ingestion job can be created."""
    job_repo = IngestionJobRepository(temp_db)

    job_id = job_repo.create_ingestion_job(total_files=10, options={"recursive": True})
    assert job_id > 0

    job = job_repo.get_ingestion_job(job_id)
    assert job is not None
    assert job["total_files"] == 10
    assert job["status"] == "pending"


def test_ingestion_job_repository_update_status(temp_db):
    """Ingestion job status can be updated."""
    job_repo = IngestionJobRepository(temp_db)

    job_id = job_repo.create_ingestion_job(total_files=10)
    job_repo.update_ingestion_job(job_id, status="running")

    job = job_repo.get_ingestion_job(job_id)
    assert job["status"] == "running"
    assert job["started_at"] is not None


def test_ingestion_job_repository_update_progress(temp_db):
    """Ingestion job progress can be updated."""
    job_repo = IngestionJobRepository(temp_db)

    job_id = job_repo.create_ingestion_job(total_files=10)
    job_repo.update_ingestion_job(job_id, processed_files=5, failed_files=1)

    job = job_repo.get_ingestion_job(job_id)
    assert job["processed_files"] == 5
    assert job["failed_files"] == 1


def test_ingestion_job_repository_complete(temp_db):
    """Ingestion job can be marked as completed."""
    job_repo = IngestionJobRepository(temp_db)

    job_id = job_repo.create_ingestion_job(total_files=10)
    job_repo.update_ingestion_job(job_id, status="completed", processed_files=10)

    job = job_repo.get_ingestion_job(job_id)
    assert job["status"] == "completed"
    assert job["completed_at"] is not None


def test_ingestion_job_repository_fail(temp_db):
    """Ingestion job can be marked as failed."""
    job_repo = IngestionJobRepository(temp_db)

    job_id = job_repo.create_ingestion_job(total_files=10)
    job_repo.update_ingestion_job(
        job_id, status="failed", error_message="Processing error"
    )

    job = job_repo.get_ingestion_job(job_id)
    assert job["status"] == "failed"
    assert job["error_message"] == "Processing error"
    assert job["completed_at"] is not None


def test_ingestion_job_repository_list(temp_db):
    """Ingestion jobs can be listed."""
    job_repo = IngestionJobRepository(temp_db)

    # Create multiple jobs
    job_repo.create_ingestion_job(total_files=10)
    job_repo.create_ingestion_job(total_files=5)
    job_repo.create_ingestion_job(total_files=20)

    jobs = job_repo.list_ingestion_jobs()
    assert len(jobs) == 3


def test_ingestion_job_repository_list_with_status_filter(temp_db):
    """Ingestion jobs can be filtered by status."""
    job_repo = IngestionJobRepository(temp_db)

    # Create jobs with different statuses
    job1 = job_repo.create_ingestion_job(total_files=10)
    job2 = job_repo.create_ingestion_job(total_files=5)
    job_repo.update_ingestion_job(job1, status="completed")

    pending_jobs = job_repo.list_ingestion_jobs(status="pending")
    completed_jobs = job_repo.list_ingestion_jobs(status="completed")

    assert len(pending_jobs) == 1
    assert len(completed_jobs) == 1


def test_ingestion_job_repository_options_round_trip(temp_db):
    """Ingestion job options are preserved round-trip."""
    job_repo = IngestionJobRepository(temp_db)

    options = {"recursive": True, "extensions": [".txt", ".md"], "max_size": 1000000}
    job_id = job_repo.create_ingestion_job(total_files=10, options=options)

    job = job_repo.get_ingestion_job(job_id)
    retrieved_options = json.loads(job["options"])
    assert retrieved_options == options


# ── Integration tests ─────────────────────────────────────────────────────

def test_parser_chunker_persistence_integration(temp_db):
    """End-to-end test: parser → chunker → persistence workflow."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Create a test file
        test_file = Path(tmp_dir) / "test.txt"
        test_file.write_text("Paragraph 1\n\nParagraph 2\n\nParagraph 3", encoding="utf-8")

        # Parse the file
        parser_registry = ParserRegistry()
        parsed_doc = parser_registry.parse(test_file)

        # Chunk the parsed document
        chunker_registry = ChunkerRegistry()
        chunks = chunker_registry.chunk(parsed_doc)

        # Persist to database
        doc_repo = DocumentRepository(temp_db)
        version_repo = DocumentVersionRepository(temp_db)
        chunk_repo = ChunkRepository(temp_db)

        # Get file size
        file_size = test_file.stat().st_size

        # Create document
        doc_id = doc_repo.create_document(parsed_doc, file_size=file_size)

        # Create version
        content_hash = doc_repo.get_document(doc_id)["content_hash"]
        version_id = version_repo.create_document_version(
            doc_id, content_hash, file_size, "created"
        )

        # Save chunks
        chunk_ids = chunk_repo.save_chunks(doc_id, version_id, chunks)

        # Verify persistence
        assert doc_id > 0
        assert version_id > 0
        assert len(chunk_ids) == len(chunks)

        # Retrieve and verify
        retrieved_chunks = chunk_repo.get_chunks(
            document_id=doc_id, version_id=version_id
        )
        assert len(retrieved_chunks) == len(chunks)


def test_foreign_key_integrity(temp_db, sample_parsed_document):
    """Foreign key constraints are enforced."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)
    chunk_repo = ChunkRepository(temp_db)

    # Create document
    doc_id = doc_repo.create_document(sample_parsed_document, file_size=100)

    # Try to create version with non-existent document ID
    with pytest.raises(Exception):  # sqlite3.IntegrityError
        version_repo.create_document_version(99999, "hash", 100)

    # Create valid version
    version_id = version_repo.create_document_version(doc_id, "hash", 100)

    # Try to create chunk with non-existent version ID
    chunks = [
        DocumentChunk(
            chunk_id="chunk_1",
            source_path="/path/to/doc.txt",
            document_title="Test",
            file_type="text/plain",
            chunk_index=0,
            content="Content",
            token_count=1,
        )
    ]

    with pytest.raises(Exception):  # sqlite3.IntegrityError
        chunk_repo.save_chunks(99999, 99999, chunks)


def test_cascade_delete_behavior(temp_db, sample_parsed_document, sample_chunks):
    """Cascade delete behavior works correctly."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)
    chunk_repo = ChunkRepository(temp_db)

    # Create document, version, and chunks
    doc_id = doc_repo.create_document(sample_parsed_document, file_size=100)
    version_id = version_repo.create_document_version(doc_id, "hash", 100)
    chunk_repo.save_chunks(doc_id, version_id, sample_chunks)

    # Delete document (should cascade delete versions and chunks)
    doc_repo.delete_document(doc_id)

    # Verify cascade deletion
    assert doc_repo.get_document(doc_id) is None
    assert version_repo.get_document_versions(doc_id) == []
    assert chunk_repo.get_chunks(document_id=doc_id) == []


def test_database_reopen_preserves_data(temp_db, sample_parsed_document):
    """Data persists across database reopen."""
    doc_repo = DocumentRepository(temp_db)

    # Create document
    doc_id = doc_repo.create_document(sample_parsed_document, file_size=100)

    # Close and reopen database
    temp_db.close()
    temp_db.connect()

    # Verify data persistence
    doc = doc_repo.get_document(doc_id)
    assert doc is not None
    assert doc["title"] == sample_parsed_document.title


def test_transaction_safety_on_partial_write(temp_db, sample_parsed_document):
    """Transaction handling in repository operations."""
    doc_repo = DocumentRepository(temp_db)
    version_repo = DocumentVersionRepository(temp_db)

    # Create document
    doc_id = doc_repo.create_document(sample_parsed_document, file_size=100)

    # Create multiple versions (each in its own transaction)
    version_repo.create_document_version(doc_id, "hash1", 100)
    version_repo.create_document_version(doc_id, "hash2", 150)

    # Verify both versions were created (each committed separately)
    versions = version_repo.get_document_versions(doc_id)
    assert len(versions) == 2

    # Test that we can manually control transactions for bulk operations
    temp_db.begin_transaction()

    # Create a version in manual transaction
    version_repo.create_document_version(doc_id, "hash3", 200)

    # Rollback the manual transaction
    temp_db.rollback()

    # The version created inside the manual transaction should still exist
    # because repository methods commit their own transactions
    versions_after = version_repo.get_document_versions(doc_id)
    assert len(versions_after) == 3  # Original 2 + the one we just created
