from __future__ import annotations

import sqlite3
from typing import Final

from truecoder.execution.errors import AuditUnavailableError

AUDIT_SCHEMA_VERSION: Final = 1

_SCHEMA_OBJECTS: Final = frozenset(
    {
        "audit_schema",
        "audit_runs",
        "audit_resources",
        "audit_events",
        "audit_events_run_sequence",
        "audit_one_terminal_event",
        "audit_nonterminal_recovery",
        "audit_events_no_update",
        "audit_events_no_delete",
        "audit_events_stop_after_terminal",
        "audit_runs_no_delete",
        "audit_runs_terminal_immutable",
        "audit_resources_no_update",
        "audit_resources_no_delete",
        "audit_resources_not_after_terminal",
    }
)

_SCHEMA_SQL: Final = """
BEGIN IMMEDIATE;

CREATE TABLE audit_schema (
    version INTEGER PRIMARY KEY,
    installed_at TEXT NOT NULL
);

CREATE TABLE audit_runs (
    run_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL UNIQUE,
    tool_call_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    request_summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    phase TEXT NOT NULL
        CHECK (phase IN ('pending', 'running', 'terminal')),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    start_json TEXT,
    finalization_json TEXT,
    outcome TEXT,
    next_event_sequence INTEGER NOT NULL DEFAULT 0
        CHECK (next_event_sequence >= 0),
    recovery_owner TEXT,
    recovery_lease_until TEXT,
    stdout_sha256 TEXT,
    stderr_sha256 TEXT,
    stdout_bytes INTEGER NOT NULL DEFAULT 0 CHECK (stdout_bytes >= 0),
    stderr_bytes INTEGER NOT NULL DEFAULT 0 CHECK (stderr_bytes >= 0),
    stdout_preview TEXT NOT NULL DEFAULT '',
    stderr_preview TEXT NOT NULL DEFAULT '',
    stdout_truncated INTEGER NOT NULL DEFAULT 0
        CHECK (stdout_truncated IN (0, 1)),
    stderr_truncated INTEGER NOT NULL DEFAULT 0
        CHECK (stderr_truncated IN (0, 1)),
    output_complete INTEGER NOT NULL DEFAULT 1
        CHECK (output_complete IN (0, 1)),
    CHECK (
        (phase = 'pending' AND start_json IS NULL
            AND finalization_json IS NULL AND outcome IS NULL)
        OR
        (phase = 'running' AND start_json IS NOT NULL
            AND finalization_json IS NULL AND outcome IS NULL)
        OR
        (phase = 'terminal' AND finalization_json IS NOT NULL
            AND outcome IS NOT NULL)
    ),
    CHECK (
        (recovery_owner IS NULL AND recovery_lease_until IS NULL)
        OR
        (recovery_owner IS NOT NULL AND recovery_lease_until IS NOT NULL)
    )
);

CREATE TABLE audit_resources (
    run_id TEXT PRIMARY KEY
        REFERENCES audit_runs(run_id),
    resource_json TEXT NOT NULL,
    attached_at TEXT NOT NULL
);

CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL
        REFERENCES audit_runs(run_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    occurred_at TEXT NOT NULL,
    phase TEXT NOT NULL
        CHECK (phase IN ('pending', 'running', 'terminal')),
    event_type TEXT NOT NULL,
    message TEXT,
    metadata_json TEXT NOT NULL,
    terminal INTEGER NOT NULL CHECK (terminal IN (0, 1)),
    UNIQUE (run_id, sequence)
);

CREATE INDEX audit_events_run_sequence
    ON audit_events(run_id, sequence);

CREATE UNIQUE INDEX audit_one_terminal_event
    ON audit_events(run_id)
    WHERE terminal = 1;

CREATE INDEX audit_nonterminal_recovery
    ON audit_runs(phase, recovery_lease_until, created_at)
    WHERE phase != 'terminal';

CREATE TRIGGER audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are immutable');
END;

CREATE TRIGGER audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are immutable');
END;

CREATE TRIGGER audit_events_stop_after_terminal
BEFORE INSERT ON audit_events
WHEN EXISTS (
    SELECT 1 FROM audit_events
    WHERE run_id = NEW.run_id AND terminal = 1
)
BEGIN
    SELECT RAISE(ABORT, 'terminal audit event already exists');
END;

CREATE TRIGGER audit_runs_no_delete
BEFORE DELETE ON audit_runs
BEGIN
    SELECT RAISE(ABORT, 'audit runs are immutable evidence');
END;

CREATE TRIGGER audit_runs_terminal_immutable
BEFORE UPDATE ON audit_runs
WHEN OLD.phase = 'terminal'
BEGIN
    SELECT RAISE(ABORT, 'terminal audit run is immutable');
END;

CREATE TRIGGER audit_resources_no_update
BEFORE UPDATE ON audit_resources
BEGIN
    SELECT RAISE(ABORT, 'audit resource identifiers are immutable');
END;

CREATE TRIGGER audit_resources_no_delete
BEFORE DELETE ON audit_resources
BEGIN
    SELECT RAISE(ABORT, 'audit resource identifiers are immutable');
END;

CREATE TRIGGER audit_resources_not_after_terminal
BEFORE INSERT ON audit_resources
WHEN (
    SELECT phase FROM audit_runs WHERE run_id = NEW.run_id
) = 'terminal'
BEGIN
    SELECT RAISE(ABORT, 'cannot attach a resource to a terminal run');
END;

INSERT INTO audit_schema(version, installed_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 1;
COMMIT;
"""


def configure_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = FULL")
    journal_mode = str(
        connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    ).lower()
    if journal_mode != "wal":
        raise AuditUnavailableError(
            f"audit database could not enable WAL mode: {journal_mode}",
            operation="initialize_audit_schema",
        )


def initialize_schema(connection: sqlite3.Connection) -> None:
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            connection.executescript(_SCHEMA_SQL)
        elif version != AUDIT_SCHEMA_VERSION:
            raise AuditUnavailableError(
                f"unsupported audit database version: {version}",
                operation="initialize_audit_schema",
            )
        verify_schema(connection)
    except AuditUnavailableError:
        raise
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.rollback()
        raise AuditUnavailableError(
            f"could not initialize the audit schema: {error}",
            operation="initialize_audit_schema",
        ) from error


def verify_schema(connection: sqlite3.Connection) -> None:
    version_row = connection.execute("SELECT version FROM audit_schema").fetchone()
    if version_row is None or int(version_row["version"]) != AUDIT_SCHEMA_VERSION:
        raise AuditUnavailableError(
            "audit schema metadata is missing or unsupported",
            operation="verify_audit_schema",
        )

    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE name LIKE 'audit_%'
          AND type IN ('table', 'index', 'trigger')
        """
    ).fetchall()
    actual = {str(row["name"]) for row in rows}
    missing = _SCHEMA_OBJECTS - actual
    if missing:
        raise AuditUnavailableError(
            f"audit schema is incomplete: {', '.join(sorted(missing))}",
            operation="verify_audit_schema",
        )
