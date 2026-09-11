from __future__ import annotations

import sqlite3
from pathlib import Path

from .schema import KnowledgeSchema


class KnowledgeDatabase:
    """SQLite database connection manager for JARVIS V4 knowledge persistence.

    Manages database connections, schema initialization, and provides
    a clean interface for repository operations.
    """

    def __init__(self, db_path: str | Path):
        """Initialize knowledge database connection.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> sqlite3.Connection:
        """Get or create database connection.

        Returns:
            SQLite database connection with foreign keys enabled.
        """
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row  # Enable dict-like row access
            self._initialize_schema()
        return self._conn

    def close(self) -> None:
        """Close database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> sqlite3.Connection:
        """Context manager entry."""
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()

    def _initialize_schema(self) -> None:
        """Initialize database schema if needed."""
        if self._conn is None:
            raise RuntimeError("Database connection not established")

        # Check if schema already exists
        schema_version = KnowledgeSchema.get_schema_version(self._conn)
        if schema_version is None:
            # Create fresh schema
            KnowledgeSchema.create_tables(self._conn)
            KnowledgeSchema.set_schema_version(self._conn, 1)
        elif schema_version < 1:
            # Future migration logic would go here
            KnowledgeSchema.create_tables(self._conn)
            KnowledgeSchema.set_schema_version(self._conn, 1)

    def begin_transaction(self) -> None:
        """Begin a new transaction."""
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        self._conn.execute("BEGIN")

    def commit(self) -> None:
        """Commit current transaction."""
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        self._conn.commit()

    def rollback(self) -> None:
        """Rollback current transaction."""
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        self._conn.rollback()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute SQL statement with parameters.

        Args:
            sql: SQL statement with parameter placeholders.
            params: Parameter values.

        Returns:
            SQLite cursor.
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, params_list: list[tuple]) -> sqlite3.Cursor:
        """Execute SQL statement with multiple parameter sets.

        Args:
            sql: SQL statement with parameter placeholders.
            params_list: List of parameter tuples.

        Returns:
            SQLite cursor.
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        return self._conn.executemany(sql, params_list)

    def vacuum(self) -> None:
        """Run VACUUM to optimize database size."""
        if self._conn is None:
            raise RuntimeError("Database connection not established")
        self._conn.execute("VACUUM")
        self._conn.commit()

    def backup(self, backup_path: str | Path) -> None:
        """Create a backup of the database.

        Args:
            backup_path: Path for backup database file.
        """
        if self._conn is None:
            raise RuntimeError("Database connection not established")

        backup_path = Path(backup_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)

        # Use SQLite backup API
        backup = sqlite3.connect(str(backup_path))
        self._conn.backup(backup)
        backup.close()
