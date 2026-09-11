from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..chunkers.models import DocumentChunk
from ..parsers.base import ParsedDocument
from .database import KnowledgeDatabase


class DocumentRepository:
    """Repository for document persistence operations."""

    def __init__(self, db: KnowledgeDatabase):
        """Initialize document repository.

        Args:
            db: Knowledge database connection.
        """
        self.db = db

    def create_document(
        self,
        parsed_doc: ParsedDocument,
        file_size: int,
        relative_path: Optional[str] = None,
    ) -> int:
        """Create a new document record.

        Args:
            parsed_doc: Parsed document from parser.
            file_size: Size of the source file in bytes.
            relative_path: Optional relative path for storage organization.

        Returns:
            Document ID.

        Raises:
            sqlite3.IntegrityError: If document with same source_path and content_hash exists.
        """
        conn = self.db.connect()

        # Calculate content hash
        content_hash = self._calculate_content_hash(parsed_doc.text)

        # Serialize metadata
        metadata_json = json.dumps(parsed_doc.metadata) if parsed_doc.metadata else None

        # Get file extension
        extension = Path(parsed_doc.source_path).suffix.lower()

        sql = """
            INSERT INTO documents (
                source_path, relative_path, content_hash, file_size,
                file_type, extension, title, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """

        cursor = conn.execute(
            sql,
            (
                parsed_doc.source_path,
                relative_path,
                content_hash,
                file_size,
                parsed_doc.file_type,
                extension,
                parsed_doc.title,
                metadata_json,
            ),
        )

        document_id = cursor.lastrowid
        conn.commit()

        return document_id

    def get_document(self, document_id: int) -> Optional[dict]:
        """Get document by ID.

        Args:
            document_id: Document ID.

        Returns:
            Document dict or None if not found.
        """
        conn = self.db.connect()

        sql = "SELECT * FROM documents WHERE id = ?"
        cursor = conn.execute(sql, (document_id,))
        row = cursor.fetchone()

        if row:
            return dict(row)
        return None

    def get_document_by_source_path(self, source_path: str) -> Optional[dict]:
        """Get document by source path.

        Args:
            source_path: Source file path.

        Returns:
            Document dict or None if not found.
        """
        conn = self.db.connect()

        sql = "SELECT * FROM documents WHERE source_path = ?"
        cursor = conn.execute(sql, (source_path,))
        row = cursor.fetchone()

        if row:
            return dict(row)
        return None

    def get_document_by_content_hash(self, content_hash: str) -> Optional[dict]:
        """Get document by content hash.

        Args:
            content_hash: Content hash string.

        Returns:
            Document dict or None if not found.
        """
        conn = self.db.connect()

        sql = "SELECT * FROM documents WHERE content_hash = ?"
        cursor = conn.execute(sql, (content_hash,))
        row = cursor.fetchone()

        if row:
            return dict(row)
        return None

    def list_documents(
        self,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict]:
        """List documents with optional filtering.

        Args:
            status: Optional status filter.
            limit: Optional limit on number of results.
            offset: Offset for pagination.

        Returns:
            List of document dicts.
        """
        conn = self.db.connect()

        sql = "SELECT * FROM documents"
        params = []

        if status:
            sql += " WHERE status = ?"
            params.append(status)

        sql += " ORDER BY created_at DESC"

        if limit:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])

        cursor = conn.execute(sql, tuple(params))
        rows = cursor.fetchall()

        return [dict(row) for row in rows]

    def update_document_status(self, document_id: int, status: str) -> None:
        """Update document status.

        Args:
            document_id: Document ID.
            status: New status value.
        """
        conn = self.db.connect()

        sql = "UPDATE documents SET status = ?, modified_at = CURRENT_TIMESTAMP WHERE id = ?"
        conn.execute(sql, (status, document_id))
        conn.commit()

    def delete_document(self, document_id: int) -> bool:
        """Delete document by ID.

        This will cascade delete associated versions and chunks.

        Args:
            document_id: Document ID.

        Returns:
            True if deleted, False if not found.
        """
        conn = self.db.connect()

        sql = "DELETE FROM documents WHERE id = ?"
        cursor = conn.execute(sql, (document_id,))
        conn.commit()

        return cursor.rowcount > 0

    def _calculate_content_hash(self, content: str) -> str:
        """Calculate SHA256 hash of content.

        Args:
            content: Text content.

        Returns:
            Hexadecimal hash string.
        """
        return hashlib.sha256(content.encode()).hexdigest()


class DocumentVersionRepository:
    """Repository for document version persistence operations."""

    def __init__(self, db: KnowledgeDatabase):
        """Initialize document version repository.

        Args:
            db: Knowledge database connection.
        """
        self.db = db

    def create_document_version(
        self,
        document_id: int,
        content_hash: str,
        file_size: int,
        change_type: str = "created",
        metadata: Optional[dict] = None,
    ) -> int:
        """Create a new document version.

        Args:
            document_id: Document ID.
            content_hash: Content hash of this version.
            file_size: File size in bytes.
            change_type: Type of change (created, modified, etc.).
            metadata: Optional version metadata.

        Returns:
            Version ID.
        """
        conn = self.db.connect()

        # Get next version number
        version_number = self._get_next_version_number(document_id)

        # Serialize metadata
        metadata_json = json.dumps(metadata) if metadata else None

        sql = """
            INSERT INTO document_versions (
                document_id, version_number, content_hash, file_size,
                change_type, metadata
            ) VALUES (?, ?, ?, ?, ?, ?)
        """

        cursor = conn.execute(
            sql,
            (
                document_id,
                version_number,
                content_hash,
                file_size,
                change_type,
                metadata_json,
            ),
        )

        version_id = cursor.lastrowid
        conn.commit()

        return version_id

    def get_document_versions(self, document_id: int) -> list[dict]:
        """Get all versions of a document.

        Args:
            document_id: Document ID.

        Returns:
            List of version dicts ordered by version number.
        """
        conn = self.db.connect()

        sql = """
            SELECT * FROM document_versions
            WHERE document_id = ?
            ORDER BY version_number ASC
        """

        cursor = conn.execute(sql, (document_id,))
        rows = cursor.fetchall()

        return [dict(row) for row in rows]

    def get_latest_version(self, document_id: int) -> Optional[dict]:
        """Get the latest version of a document.

        Args:
            document_id: Document ID.

        Returns:
            Latest version dict or None if not found.
        """
        conn = self.db.connect()

        sql = """
            SELECT * FROM document_versions
            WHERE document_id = ?
            ORDER BY version_number DESC
            LIMIT 1
        """

        cursor = conn.execute(sql, (document_id,))
        row = cursor.fetchone()

        if row:
            return dict(row)
        return None

    def _get_next_version_number(self, document_id: int) -> int:
        """Get the next version number for a document.

        Args:
            document_id: Document ID.

        Returns:
            Next version number (1 if no versions exist).
        """
        conn = self.db.connect()

        sql = """
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM document_versions
            WHERE document_id = ?
        """

        cursor = conn.execute(sql, (document_id,))
        result = cursor.fetchone()

        return result[0] if result else 1


class ChunkRepository:
    """Repository for chunk persistence operations."""

    def __init__(self, db: KnowledgeDatabase):
        """Initialize chunk repository.

        Args:
            db: Knowledge database connection.
        """
        self.db = db

    def save_chunks(
        self,
        document_id: int,
        version_id: int,
        chunks: list[DocumentChunk],
    ) -> list[int]:
        """Save chunks for a document version.

        Args:
            document_id: Document ID.
            version_id: Document version ID.
            chunks: List of DocumentChunk objects.

        Returns:
            List of chunk IDs in the same order as input chunks.

        Raises:
            ValueError: If chunk indices are not sequential starting from 0.
        """
        # Validate chunk ordering
        for i, chunk in enumerate(chunks):
            if chunk.chunk_index != i:
                raise ValueError(
                    f"Chunk indices must be sequential starting from 0. "
                    f"Expected {i}, got {chunk.chunk_index}"
                )

        conn = self.db.connect()
        chunk_ids = []

        sql = """
            INSERT INTO chunks (
                document_id, version_id, chunk_index, content,
                content_hash, token_count, source_location, heading_metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """

        for chunk in chunks:
            # Calculate content hash
            content_hash = self._calculate_content_hash(chunk.content)

            # Serialize location metadata
            source_location_json = (
                json.dumps(chunk.metadata.get("source_location"))
                if chunk.metadata.get("source_location")
                else None
            )

            # Serialize heading metadata
            heading_metadata_json = (
                json.dumps(
                    {
                        "heading": chunk.metadata.get("heading"),
                        "heading_level": chunk.metadata.get("heading_level"),
                        "section_path": chunk.metadata.get("section_path"),
                    }
                )
                if chunk.metadata.get("heading")
                else None
            )

            cursor = conn.execute(
                sql,
                (
                    document_id,
                    version_id,
                    chunk.chunk_index,
                    chunk.content,
                    content_hash,
                    chunk.token_count,
                    source_location_json,
                    heading_metadata_json,
                ),
            )

            chunk_ids.append(cursor.lastrowid)

        conn.commit()
        return chunk_ids

    def get_chunks(
        self,
        document_id: Optional[int] = None,
        version_id: Optional[int] = None,
    ) -> list[dict]:
        """Get chunks with optional filtering.

        Args:
            document_id: Optional document ID filter.
            version_id: Optional version ID filter.

        Returns:
            List of chunk dicts ordered by chunk_index.
        """
        conn = self.db.connect()

        sql = "SELECT * FROM chunks"
        params = []
        conditions = []

        if document_id:
            conditions.append("document_id = ?")
            params.append(document_id)

        if version_id:
            conditions.append("version_id = ?")
            params.append(version_id)

        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        sql += " ORDER BY chunk_index ASC"

        cursor = conn.execute(sql, tuple(params))
        rows = cursor.fetchall()

        return [dict(row) for row in rows]

    def get_chunk(self, chunk_id: int) -> Optional[dict]:
        """Get chunk by ID.

        Args:
            chunk_id: Chunk ID.

        Returns:
            Chunk dict or None if not found.
        """
        conn = self.db.connect()

        sql = "SELECT * FROM chunks WHERE id = ?"
        cursor = conn.execute(sql, (chunk_id,))
        row = cursor.fetchone()

        if row:
            return dict(row)
        return None

    def delete_chunks_by_version(self, version_id: int) -> int:
        """Delete all chunks for a document version.

        Args:
            version_id: Document version ID.

        Returns:
            Number of chunks deleted.
        """
        conn = self.db.connect()

        sql = "DELETE FROM chunks WHERE version_id = ?"
        cursor = conn.execute(sql, (version_id,))
        conn.commit()

        return cursor.rowcount

    def _calculate_content_hash(self, content: str) -> str:
        """Calculate SHA256 hash of content.

        Args:
            content: Text content.

        Returns:
            Hexadecimal hash string.
        """
        return hashlib.sha256(content.encode()).hexdigest()


class IngestionJobRepository:
    """Repository for ingestion job persistence operations."""

    def __init__(self, db: KnowledgeDatabase):
        """Initialize ingestion job repository.

        Args:
            db: Knowledge database connection.
        """
        self.db = db

    def create_ingestion_job(
        self,
        total_files: int = 0,
        options: Optional[dict] = None,
    ) -> int:
        """Create a new ingestion job.

        Args:
            total_files: Total number of files to process.
            options: Optional job configuration metadata.

        Returns:
            Job ID.
        """
        conn = self.db.connect()

        # Serialize options
        options_json = json.dumps(options) if options else None

        sql = """
            INSERT INTO ingestion_jobs (total_files, options)
            VALUES (?, ?)
        """

        cursor = conn.execute(sql, (total_files, options_json))
        job_id = cursor.lastrowid
        conn.commit()

        return job_id

    def update_ingestion_job(
        self,
        job_id: int,
        status: Optional[str] = None,
        processed_files: Optional[int] = None,
        failed_files: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Update ingestion job status and progress.

        Args:
            job_id: Job ID.
            status: Optional new status.
            processed_files: Optional processed file count.
            failed_files: Optional failed file count.
            error_message: Optional error message.
        """
        conn = self.db.connect()

        updates = []
        params = []

        if status is not None:
            updates.append("status = ?")
            params.append(status)

            # Set timestamps based on status
            if status == "running":
                updates.append("started_at = CURRENT_TIMESTAMP")
            elif status in ("completed", "failed"):
                updates.append("completed_at = CURRENT_TIMESTAMP")

        if processed_files is not None:
            updates.append("processed_files = ?")
            params.append(processed_files)

        if failed_files is not None:
            updates.append("failed_files = ?")
            params.append(failed_files)

        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)

        if not updates:
            return

        params.append(job_id)
        sql = f"UPDATE ingestion_jobs SET {', '.join(updates)} WHERE id = ?"

        conn.execute(sql, tuple(params))
        conn.commit()

    def get_ingestion_job(self, job_id: int) -> Optional[dict]:
        """Get ingestion job by ID.

        Args:
            job_id: Job ID.

        Returns:
            Job dict or None if not found.
        """
        conn = self.db.connect()

        sql = "SELECT * FROM ingestion_jobs WHERE id = ?"
        cursor = conn.execute(sql, (job_id,))
        row = cursor.fetchone()

        if row:
            return dict(row)
        return None

    def list_ingestion_jobs(
        self,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """List ingestion jobs with optional filtering.

        Args:
            status: Optional status filter.
            limit: Optional limit on number of results.

        Returns:
            List of job dicts ordered by created_at DESC.
        """
        conn = self.db.connect()

        sql = "SELECT * FROM ingestion_jobs"
        params = []

        if status:
            sql += " WHERE status = ?"
            params.append(status)

        sql += " ORDER BY created_at DESC"

        if limit:
            sql += " LIMIT ?"
            params.append(limit)

        cursor = conn.execute(sql, tuple(params))
        rows = cursor.fetchall()

        return [dict(row) for row in rows]
