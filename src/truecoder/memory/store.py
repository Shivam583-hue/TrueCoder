from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from platformdirs import user_data_path

from truecoder.execution.audit.permissions import AuditPermissions
from truecoder.execution.errors import AuditUnavailableError
from truecoder.memory.models import (
    MAX_MEMORY_ENTRIES,
    Memory,
    MemoryEntry,
    normalize_note,
)

MEMORY_SCHEMA_VERSION: Final = 1

_SCHEMA_SQL: Final = """
BEGIN IMMEDIATE;

CREATE TABLE memory_schema (
    version INTEGER PRIMARY KEY,
    installed_at TEXT NOT NULL
);

CREATE TABLE memory_entries (
    entry_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX memory_entries_workspace
    ON memory_entries(workspace_id, created_at);

CREATE UNIQUE INDEX memory_entries_unique_note
    ON memory_entries(workspace_id, note);

INSERT INTO memory_schema(version, installed_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 1;
COMMIT;
"""


def default_memory_database_path() -> Path:
    return user_data_path("truecoder", appauthor=False) / "memory.sqlite3"


class MemoryStore:
    def __init__(
        self,
        database_path: Path,
        workspace_id: str,
        *,
        permissions: AuditPermissions | None = None,
        limit: int = MAX_MEMORY_ENTRIES,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("database_path must be a pathlib.Path")
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            raise ValueError("workspace_id cannot be empty")
        if limit < 1:
            raise ValueError("limit must be at least one")

        self.database_path = Path(os.path.abspath(database_path.expanduser()))
        self.workspace_id = workspace_id
        self._permissions = permissions or AuditPermissions()
        self._limit = limit
        self._connection: sqlite3.Connection | None = None
        self.failures = 0

    def open(self) -> None:
        if self._connection is not None:
            return

        self._permissions.prepare(self.database_path)
        try:
            connection = sqlite3.connect(
                self.database_path,
                isolation_level=None,
                check_same_thread=False,
            )
        except sqlite3.Error as error:
            raise AuditUnavailableError(
                f"could not open the memory database: {error}",
                operation="open_memory",
            ) from error

        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA journal_mode = WAL")
            self._initialize(connection)
        except Exception:
            connection.close()
            raise

        self._permissions.secure_sidecars(self.database_path)
        self._connection = connection

    def remember(self, note: str) -> MemoryEntry:
        cleaned = normalize_note(note)
        self.open()
        assert self._connection is not None

        entry = MemoryEntry(
            entry_id=f"mem_{uuid.uuid4().hex}",
            workspace_id=self.workspace_id,
            note=cleaned,
            created_at=datetime.now(UTC).isoformat(),
        )
        self._connection.execute(
            """
            INSERT INTO memory_entries (entry_id, workspace_id, note, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(workspace_id, note) DO NOTHING
            """,
            (entry.entry_id, entry.workspace_id, entry.note, entry.created_at),
        )
        self._prune()
        return self.find(cleaned) or entry

    def forget(self, entry_id: str) -> bool:
        self.open()
        assert self._connection is not None

        cursor = self._connection.execute(
            "DELETE FROM memory_entries WHERE workspace_id = ? AND entry_id = ?",
            (self.workspace_id, entry_id),
        )
        return cursor.rowcount > 0

    def forget_note(self, note: str) -> bool:
        entry = self.find(note)
        return False if entry is None else self.forget(entry.entry_id)

    def find(self, note: str) -> MemoryEntry | None:
        self.open()
        assert self._connection is not None

        row = self._connection.execute(
            "SELECT * FROM memory_entries WHERE workspace_id = ? AND note = ?",
            (self.workspace_id, normalize_note(note)),
        ).fetchone()
        return None if row is None else self._entry(row)

    def entries(self) -> tuple[MemoryEntry, ...]:
        self.open()
        assert self._connection is not None

        rows = self._connection.execute(
            """
            SELECT * FROM memory_entries
            WHERE workspace_id = ?
            ORDER BY created_at ASC, rowid ASC
            """,
            (self.workspace_id,),
        ).fetchall()
        return tuple(self._entry(row) for row in rows)

    def load(self) -> Memory:
        return Memory(entries=self.entries())

    def clear(self) -> int:
        self.open()
        assert self._connection is not None

        cursor = self._connection.execute(
            "DELETE FROM memory_entries WHERE workspace_id = ?",
            (self.workspace_id,),
        )
        return cursor.rowcount

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _prune(self) -> None:
        assert self._connection is not None
        self._connection.execute(
            """
            DELETE FROM memory_entries
            WHERE workspace_id = ?
              AND entry_id NOT IN (
                SELECT entry_id FROM memory_entries
                WHERE workspace_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
              )
            """,
            (self.workspace_id, self.workspace_id, self._limit),
        )

    @staticmethod
    def _entry(row: sqlite3.Row) -> MemoryEntry:
        return MemoryEntry(
            entry_id=str(row["entry_id"]),
            workspace_id=str(row["workspace_id"]),
            note=str(row["note"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                connection.executescript(_SCHEMA_SQL)
            elif version != MEMORY_SCHEMA_VERSION:
                raise AuditUnavailableError(
                    f"unsupported memory database version: {version}",
                    operation="initialize_memory",
                )
        except AuditUnavailableError:
            raise
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.rollback()
            raise AuditUnavailableError(
                f"could not initialize the memory schema: {error}",
                operation="initialize_memory",
            ) from error
