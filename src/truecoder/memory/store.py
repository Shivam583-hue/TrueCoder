from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
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
    note_key,
)

MEMORY_SCHEMA_VERSION: Final = 2

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
    note_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX memory_entries_workspace
    ON memory_entries(workspace_id, created_at);

CREATE UNIQUE INDEX memory_entries_unique_key
    ON memory_entries(workspace_id, note_key);

INSERT INTO memory_schema(version, installed_at)
VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 2;
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

    def remember(self, note: str, *, replaces: str | None = None) -> MemoryEntry:
        cleaned = normalize_note(note)
        key = note_key(cleaned)
        superseded = None if replaces is None else note_key(replaces)
        self.open()
        assert self._connection is not None

        entry = MemoryEntry(
            entry_id=f"mem_{uuid.uuid4().hex}",
            workspace_id=self.workspace_id,
            note=cleaned,
            created_at=datetime.now(UTC).isoformat(),
        )

        with self._transaction():
            if superseded is not None and superseded != key:
                self._connection.execute(
                    "DELETE FROM memory_entries "
                    "WHERE workspace_id = ? AND note_key = ?",
                    (self.workspace_id, superseded),
                )
            self._connection.execute(
                """
                INSERT INTO memory_entries
                    (entry_id, workspace_id, note, note_key, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, note_key)
                DO UPDATE SET note = excluded.note
                """,
                (
                    entry.entry_id,
                    entry.workspace_id,
                    entry.note,
                    key,
                    entry.created_at,
                ),
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
            "SELECT * FROM memory_entries WHERE workspace_id = ? AND note_key = ?",
            (self.workspace_id, note_key(note)),
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

    @contextmanager
    def _transaction(self):
        assert self._connection is not None
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        self._connection.commit()

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
    def _migrate_to_keys(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT entry_id, workspace_id, note FROM memory_entries"
        ).fetchall()
        keyed = [
            (str(row["entry_id"]), str(row["workspace_id"]), note_key(row["note"]))
            for row in rows
        ]

        newest: dict[tuple[str, str], str] = {}
        for entry_id, workspace, key in keyed:
            newest[(workspace, key)] = entry_id
        survivors = set(newest.values())

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DROP INDEX IF EXISTS memory_entries_unique_note")
            connection.execute(
                "ALTER TABLE memory_entries ADD COLUMN note_key TEXT NOT NULL "
                "DEFAULT ''"
            )
            for entry_id, _workspace, key in keyed:
                if entry_id not in survivors:
                    connection.execute(
                        "DELETE FROM memory_entries WHERE entry_id = ?",
                        (entry_id,),
                    )
                    continue
                connection.execute(
                    "UPDATE memory_entries SET note_key = ? WHERE entry_id = ?",
                    (key, entry_id),
                )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS memory_entries_unique_key "
                "ON memory_entries(workspace_id, note_key)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO memory_schema(version, installed_at) "
                "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                (MEMORY_SCHEMA_VERSION,),
            )
            connection.execute(f"PRAGMA user_version = {MEMORY_SCHEMA_VERSION}")
        except BaseException:
            connection.rollback()
            raise
        connection.commit()

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                connection.executescript(_SCHEMA_SQL)
            elif version == 1:
                MemoryStore._migrate_to_keys(connection)
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
