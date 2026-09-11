from __future__ import annotations

import sqlite3
from typing import Optional


class KnowledgeSchema:
    """SQLite schema for JARVIS V4 knowledge persistence.

    Provides tables for documents, document versions, chunks, and ingestion jobs.
    Separated from Phase 3 conversation memory tables.
    """

    @staticmethod
    def create_tables(conn: sqlite3.Connection) -> None:
        """Create all knowledge persistence tables.

        Args:
            conn: SQLite database connection.
        """
        cursor = conn.cursor()

        # Enable foreign keys
        cursor.execute("PRAGMA foreign_keys = ON")

        # Documents table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL UNIQUE,
                relative_path TEXT,
                content_hash TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                file_type TEXT NOT NULL,
                extension TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                modified_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                indexed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'active',
                metadata TEXT,
                UNIQUE(source_path, content_hash)
            )
        """)

        # Indexes for documents
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_documents_source_path
            ON documents(source_path)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_documents_content_hash
            ON documents(content_hash)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_documents_status
            ON documents(status)
        """)

        # Document versions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS document_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                version_number INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                modified_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                indexed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                change_type TEXT NOT NULL DEFAULT 'created',
                metadata TEXT,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
                UNIQUE(document_id, version_number)
            )
        """)

        # Indexes for document versions
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_document_versions_document_id
            ON document_versions(document_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_document_versions_content_hash
            ON document_versions(content_hash)
        """)

        # Chunks table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                version_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                token_count INTEGER NOT NULL,
                source_location TEXT,
                heading_metadata TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
                FOREIGN KEY (version_id) REFERENCES document_versions(id) ON DELETE CASCADE,
                UNIQUE(version_id, chunk_index)
            )
        """)

        # Indexes for chunks
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_document_id
            ON chunks(document_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_version_id
            ON chunks(version_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_content_hash
            ON chunks(content_hash)
        """)

        # Ingestion jobs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ingestion_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'pending',
                total_files INTEGER NOT NULL DEFAULT 0,
                processed_files INTEGER NOT NULL DEFAULT 0,
                failed_files INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                error_message TEXT,
                options TEXT
            )
        """)

        # Indexes for ingestion jobs
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_status
            ON ingestion_jobs(status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_created_at
            ON ingestion_jobs(created_at)
        """)

        conn.commit()

    @staticmethod
    def drop_tables(conn: sqlite3.Connection) -> None:
        """Drop all knowledge persistence tables.

        WARNING: This will delete all data. Use only for testing.

        Args:
            conn: SQLite database connection.
        """
        cursor = conn.cursor()

        # Drop in reverse order to respect foreign key constraints
        cursor.execute("DROP TABLE IF EXISTS ingestion_jobs")
        cursor.execute("DROP TABLE IF EXISTS chunks")
        cursor.execute("DROP TABLE IF EXISTS document_versions")
        cursor.execute("DROP TABLE IF EXISTS documents")

        conn.commit()

    @staticmethod
    def get_schema_version(conn: sqlite3.Connection) -> Optional[int]:
        """Get the current schema version.

        Args:
            conn: SQLite database connection.

        Returns:
            Schema version number, or None if not set.
        """
        cursor = conn.cursor()
        cursor.execute("PRAGMA user_version")
        result = cursor.fetchone()
        return result[0] if result else None

    @staticmethod
    def set_schema_version(conn: sqlite3.Connection, version: int) -> None:
        """Set the schema version.

        Args:
            conn: SQLite database connection.
            version: Schema version number.
        """
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA user_version = {version}")
        conn.commit()
