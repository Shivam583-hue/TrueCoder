from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from platformdirs import user_data_path

from truecoder.execution.audit.permissions import AuditPermissions
from truecoder.execution.clock import Clock, SystemClock, validate_clock
from truecoder.execution.errors import AuditUnavailableError
from truecoder.mutation import MUTATION_KINDS, FileDiff, MutationKind
from truecoder.tools.context import ToolInvocationContext

MUTATION_SCHEMA_VERSION: Final = 1

_SCHEMA_OBJECTS: Final = frozenset(
    {
        "mutation_schema",
        "mutation_records",
        "mutation_records_workspace",
        "mutation_records_no_update",
        "mutation_records_no_delete",
    }
)

_SCHEMA_SQL: Final = """
BEGIN IMMEDIATE;

CREATE TABLE mutation_schema (
    version INTEGER PRIMARY KEY,
    installed_at TEXT NOT NULL
);

CREATE TABLE mutation_records (
    record_id TEXT PRIMARY KEY,
    tool_call_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL
        CHECK (kind IN ('create', 'replace', 'edit')),
    recorded_at TEXT NOT NULL,
    before_sha256 TEXT,
    after_sha256 TEXT NOT NULL,
    before_bytes INTEGER NOT NULL CHECK (before_bytes >= 0),
    after_bytes INTEGER NOT NULL CHECK (after_bytes >= 0),
    lines_added INTEGER NOT NULL CHECK (lines_added >= 0),
    lines_removed INTEGER NOT NULL CHECK (lines_removed >= 0),
    CHECK (
        (kind = 'create' AND before_sha256 IS NULL AND before_bytes = 0)
        OR
        (kind != 'create' AND before_sha256 IS NOT NULL)
    )
);

CREATE INDEX mutation_records_workspace
    ON mutation_records(workspace_id, recorded_at);

CREATE TRIGGER mutation_records_no_update
BEFORE UPDATE ON mutation_records
BEGIN
    SELECT RAISE(ABORT, 'mutation records are immutable evidence');
END;

CREATE TRIGGER mutation_records_no_delete
BEFORE DELETE ON mutation_records
BEGIN
    SELECT RAISE(ABORT, 'mutation records are immutable evidence');
END;

INSERT INTO mutation_schema(version, installed_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 1;
COMMIT;
"""


def default_mutation_database_path() -> Path:
    return user_data_path("truecoder", appauthor=False) / "mutations.sqlite3"


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def record_mutation(
    audit: MutationAudit | None,
    invocation: ToolInvocationContext | None,
    *,
    tool_name: str,
    path: str,
    kind: MutationKind,
    before: bytes | None,
    after: bytes,
    diff: FileDiff | None = None,
) -> MutationRecord | None:
    if audit is None or invocation is None:
        return None

    execution = invocation.execution
    return audit.record(
        tool_call_id=execution.tool_call_id,
        session_id=execution.session_id,
        turn_id=execution.turn_id,
        workspace_id=execution.workspace_id,
        tool_name=tool_name,
        path=path,
        kind=kind,
        before=before,
        after=after,
        lines_added=0 if diff is None else diff.added,
        lines_removed=0 if diff is None else diff.removed,
    )


@dataclass(frozen=True, slots=True)
class MutationRecord:
    record_id: str
    tool_call_id: str
    session_id: str
    turn_id: str
    workspace_id: str
    tool_name: str
    path: str
    kind: MutationKind
    recorded_at: str
    before_sha256: str | None
    after_sha256: str
    before_bytes: int
    after_bytes: int
    lines_added: int
    lines_removed: int

    def __post_init__(self) -> None:
        if self.kind not in MUTATION_KINDS:
            raise ValueError(f"Unsupported mutation kind: {self.kind!r}")
        if self.kind == "create" and self.before_sha256 is not None:
            raise ValueError("A created file has no prior digest.")
        if self.kind != "create" and self.before_sha256 is None:
            raise ValueError("A changed file requires its prior digest.")


class MutationAudit:
    def __init__(
        self,
        database_path: Path,
        *,
        permissions: AuditPermissions | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("database_path must be a pathlib.Path")

        self.database_path = Path(os.path.abspath(database_path.expanduser()))
        self._permissions = permissions or AuditPermissions()
        self._clock = validate_clock(clock or SystemClock())
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
                f"could not open the mutation database: {error}",
                operation="open_mutation_audit",
            ) from error

        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA synchronous = FULL")
            journal_mode = str(
                connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            ).lower()
            if journal_mode != "wal":
                raise AuditUnavailableError(
                    f"mutation database could not enable WAL mode: {journal_mode}",
                    operation="open_mutation_audit",
                )
            self._initialize(connection)
        except Exception:
            connection.close()
            raise

        self._permissions.secure_sidecars(self.database_path)
        self._connection = connection

    def record(
        self,
        *,
        tool_call_id: str,
        session_id: str,
        turn_id: str,
        workspace_id: str,
        tool_name: str,
        path: str,
        kind: MutationKind,
        before: bytes | None,
        after: bytes,
        lines_added: int,
        lines_removed: int,
    ) -> MutationRecord | None:
        record = MutationRecord(
            record_id=f"mut_{uuid.uuid4().hex}",
            tool_call_id=tool_call_id,
            session_id=session_id,
            turn_id=turn_id,
            workspace_id=workspace_id,
            tool_name=tool_name,
            path=path,
            kind=kind,
            recorded_at=self._clock.now_utc().isoformat(),
            before_sha256=None if before is None else digest(before),
            after_sha256=digest(after),
            before_bytes=0 if before is None else len(before),
            after_bytes=len(after),
            lines_added=lines_added,
            lines_removed=lines_removed,
        )

        try:
            self.open()
            assert self._connection is not None
            self._connection.execute(
                """
                INSERT INTO mutation_records (
                    record_id, tool_call_id, session_id, turn_id, workspace_id,
                    tool_name, path, kind, recorded_at, before_sha256,
                    after_sha256, before_bytes, after_bytes, lines_added,
                    lines_removed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.tool_call_id,
                    record.session_id,
                    record.turn_id,
                    record.workspace_id,
                    record.tool_name,
                    record.path,
                    record.kind,
                    record.recorded_at,
                    record.before_sha256,
                    record.after_sha256,
                    record.before_bytes,
                    record.after_bytes,
                    record.lines_added,
                    record.lines_removed,
                ),
            )
        except (AuditUnavailableError, sqlite3.Error, OSError):
            self.failures += 1
            return None

        return record

    def recent(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
    ) -> tuple[MutationRecord, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")

        self.open()
        assert self._connection is not None
        rows = self._connection.execute(
            """
            SELECT * FROM mutation_records
            WHERE workspace_id = ?
            ORDER BY recorded_at DESC, rowid DESC
            LIMIT ?
            """,
            (workspace_id, limit),
        ).fetchall()
        return tuple(
            MutationRecord(
                record_id=str(row["record_id"]),
                tool_call_id=str(row["tool_call_id"]),
                session_id=str(row["session_id"]),
                turn_id=str(row["turn_id"]),
                workspace_id=str(row["workspace_id"]),
                tool_name=str(row["tool_name"]),
                path=str(row["path"]),
                kind=str(row["kind"]),  # type: ignore[arg-type]
                recorded_at=str(row["recorded_at"]),
                before_sha256=(
                    None if row["before_sha256"] is None else str(row["before_sha256"])
                ),
                after_sha256=str(row["after_sha256"]),
                before_bytes=int(row["before_bytes"]),
                after_bytes=int(row["after_bytes"]),
                lines_added=int(row["lines_added"]),
                lines_removed=int(row["lines_removed"]),
            )
            for row in rows
        )

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                connection.executescript(_SCHEMA_SQL)
            elif version != MUTATION_SCHEMA_VERSION:
                raise AuditUnavailableError(
                    f"unsupported mutation database version: {version}",
                    operation="initialize_mutation_schema",
                )

            rows = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE name LIKE 'mutation_%'
                  AND type IN ('table', 'index', 'trigger')
                """
            ).fetchall()
            missing = _SCHEMA_OBJECTS - {str(row["name"]) for row in rows}
            if missing:
                raise AuditUnavailableError(
                    f"mutation schema is incomplete: {', '.join(sorted(missing))}",
                    operation="initialize_mutation_schema",
                )
        except AuditUnavailableError:
            raise
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.rollback()
            raise AuditUnavailableError(
                f"could not initialize the mutation schema: {error}",
                operation="initialize_mutation_schema",
            ) from error
