from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeAlias

from truecoder.execution.errors import (
    AuditPersistenceError,
    AuditUnavailableError,
    ExecutionSerializationError,
)

from .codec import (
    canonical_json,
    deserialize_audit_model,
    serialize_audit_model,
)
from .models import (
    AuditEvent,
    AuditEventType,
    AuditFinalization,
    AuditRunAdmission,
    AuditRunHandle,
    AuditRunPhase,
    AuditRunRecord,
    AuditRunSnapshot,
    AuditRunStart,
    BackendResourceIdentifier,
    Metadata,
    OutputEvidence,
)
from .permissions import AuditPermissions
from .retention import RetentionPolicy, RetentionReport, plan_retention
from .schema import configure_connection, initialize_schema, verify_schema

UtcClock: TypeAlias = Callable[[], datetime]
IdFactory: TypeAlias = Callable[[], str]


class SQLiteAuditStore:
    """Transactional, append-only storage for execution audit evidence."""

    def __init__(
        self,
        database_path: Path,
        *,
        permissions: AuditPermissions | None = None,
        clock: UtcClock | None = None,
        event_id_factory: IdFactory | None = None,
    ) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("database_path must be a pathlib.Path")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if event_id_factory is not None and not callable(event_id_factory):
            raise TypeError("event_id_factory must be callable")

        self.database_path = Path(os.path.abspath(database_path.expanduser()))
        self._permissions = permissions or AuditPermissions()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._event_id_factory = event_id_factory or (
            lambda: f"event_{uuid.uuid4().hex}"
        )
        self._access_lock = threading.RLock()
        self._initialize()

    def create_pending(self, admission: AuditRunAdmission) -> AuditRunHandle:
        if not isinstance(admission, AuditRunAdmission):
            raise TypeError("admission must be an AuditRunAdmission")

        def create(connection: sqlite3.Connection) -> AuditRunHandle:
            connection.execute(
                """
                INSERT INTO audit_runs (
                    run_id, execution_id, tool_call_id, session_id, turn_id,
                    workspace_id, request_sha256, request_summary_json,
                    created_at, updated_at, phase, revision,
                    next_event_sequence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 1)
                """,
                (
                    admission.run_id,
                    admission.execution_id,
                    admission.tool_call_id,
                    admission.session_id,
                    admission.turn_id,
                    admission.workspace_id,
                    admission.request_sha256,
                    _encode_metadata(admission.request_summary),
                    _iso(admission.created_at),
                    _iso(admission.created_at),
                ),
            )
            event = AuditEvent(
                event_id=self._new_event_id(),
                run_id=admission.run_id,
                sequence=0,
                occurred_at=admission.created_at,
                phase=AuditRunPhase.PENDING,
                event_type=AuditEventType.RUN_CREATED,
            )
            self._insert_event(connection, event)
            return AuditRunHandle(
                run_id=admission.run_id,
                execution_id=admission.execution_id,
            )

        return self._write("create_pending_audit", create)

    def append_event(
        self,
        run_id: str,
        event_type: AuditEventType,
        *,
        message: str | None = None,
        metadata: Metadata = (),
        occurred_at: datetime | None = None,
    ) -> AuditEvent:
        _required_text(run_id, "run_id")
        if not isinstance(event_type, AuditEventType):
            raise TypeError("event_type must be an AuditEventType")

        def append(connection: sqlite3.Connection) -> AuditEvent:
            row = self._run_row(connection, run_id)
            phase = AuditRunPhase(str(row["phase"]))
            if phase is AuditRunPhase.TERMINAL:
                raise AuditPersistenceError(
                    "cannot append to a terminal audit run",
                    operation="append_audit_event",
                )
            event = self._allocate_event(
                connection,
                row,
                event_type,
                occurred_at=occurred_at,
                message=message,
                metadata=metadata,
            )
            return event

        return self._write("append_audit_event", append)

    def attach_resource(
        self,
        run_id: str,
        resource: BackendResourceIdentifier,
        *,
        attached_at: datetime | None = None,
    ) -> BackendResourceIdentifier:
        _required_text(run_id, "run_id")
        if not isinstance(resource, BackendResourceIdentifier):
            raise TypeError("resource must be a BackendResourceIdentifier")
        timestamp = attached_at or self._now()

        def attach(connection: sqlite3.Connection) -> BackendResourceIdentifier:
            row = self._run_row(connection, run_id)
            if AuditRunPhase(str(row["phase"])) is AuditRunPhase.TERMINAL:
                raise AuditPersistenceError(
                    "cannot attach a resource to a terminal audit run",
                    operation="attach_audit_resource",
                )
            existing = self._resource_for(connection, run_id)
            if existing is not None:
                if existing == resource:
                    return existing
                raise AuditPersistenceError(
                    "an audit resource is already attached to this run",
                    operation="attach_audit_resource",
                )
            connection.execute(
                """
                INSERT INTO audit_resources(run_id, resource_json, attached_at)
                VALUES (?, ?, ?)
                """,
                (run_id, serialize_audit_model(resource), _iso(timestamp)),
            )
            self._allocate_event(
                connection,
                row,
                AuditEventType.RESOURCE_RESERVED,
                occurred_at=timestamp,
                metadata=(
                    ("backend", resource.backend),
                    ("resource_kind", resource.resource_kind),
                    ("resource_id", resource.resource_id),
                ),
            )
            return resource

        return self._write("attach_audit_resource", attach)

    def mark_running(self, start: AuditRunStart) -> AuditRunRecord:
        if not isinstance(start, AuditRunStart):
            raise TypeError("start must be an AuditRunStart")

        def transition(connection: sqlite3.Connection) -> AuditRunRecord:
            row = self._run_row(connection, start.run_id)
            record = self._record_from_row(row)
            if record.phase is AuditRunPhase.RUNNING:
                if record.start == start:
                    return record
                raise AuditPersistenceError(
                    "audit run is already running with different start evidence",
                    operation="mark_audit_running",
                )
            if start.resource is None:
                raise AuditPersistenceError(
                    "running audit records require an exact backend resource",
                    operation="mark_audit_running",
                )
            resource = self._resource_for(connection, start.run_id)
            if resource != start.resource:
                raise AuditPersistenceError(
                    "start resource must match the durably attached resource",
                    operation="mark_audit_running",
                )
            updated = record.mark_running(start)
            connection.execute(
                """
                UPDATE audit_runs
                SET phase = 'running', start_json = ?, updated_at = ?,
                    revision = ?
                WHERE run_id = ?
                """,
                (
                    serialize_audit_model(start),
                    _iso(updated.updated_at),
                    updated.revision,
                    start.run_id,
                ),
            )
            current = self._run_row(connection, start.run_id)
            self._allocate_event(
                connection,
                current,
                AuditEventType.BACKEND_STARTED,
                occurred_at=start.started_at,
                metadata=start.metadata,
            )
            return updated

        return self._write("mark_audit_running", transition)

    def finalize(self, finalization: AuditFinalization) -> AuditRunRecord:
        if not isinstance(finalization, AuditFinalization):
            raise TypeError("finalization must be an AuditFinalization")

        def transition(connection: sqlite3.Connection) -> AuditRunRecord:
            row = self._run_row(connection, finalization.run_id)
            record = self._record_from_row(row)
            if record.phase is AuditRunPhase.TERMINAL:
                if record.finalization == finalization:
                    return record
                raise AuditPersistenceError(
                    "audit run already has a different terminal state",
                    operation="finalize_audit_run",
                )
            if record.phase is AuditRunPhase.PENDING and finalization.command_started:
                raise AuditPersistenceError(
                    "a pending audit run cannot finalize as command_started",
                    operation="finalize_audit_run",
                )
            if record.phase is AuditRunPhase.RUNNING and (
                finalization.command_started is False
            ):
                raise AuditPersistenceError(
                    "a running audit run cannot finalize as never started",
                    operation="finalize_audit_run",
                )
            resource = self._resource_for(connection, finalization.run_id)
            if (
                record.phase is AuditRunPhase.RUNNING
                and finalization.resource != resource
            ):
                raise AuditPersistenceError(
                    "a running finalization must preserve its resource evidence",
                    operation="finalize_audit_run",
                )
            if finalization.resource is not None and resource != finalization.resource:
                raise AuditPersistenceError(
                    "finalization resource does not match durable resource evidence",
                    operation="finalize_audit_run",
                )

            updated = record.mark_terminal(finalization)
            current = self._run_row(connection, finalization.run_id)
            self._allocate_event(
                connection,
                current,
                AuditEventType.RUN_FINALIZED,
                occurred_at=finalization.finalized_at,
                metadata=(("outcome", finalization.outcome.value),),
                message=finalization.detail,
                terminal=True,
            )
            output = finalization.output or OutputEvidence()
            connection.execute(
                """
                UPDATE audit_runs
                SET phase = 'terminal', finalization_json = ?, outcome = ?,
                    updated_at = ?, revision = ?, recovery_owner = NULL,
                    recovery_lease_until = NULL, stdout_sha256 = ?,
                    stderr_sha256 = ?, stdout_bytes = ?, stderr_bytes = ?,
                    stdout_preview = ?, stderr_preview = ?,
                    stdout_truncated = ?, stderr_truncated = ?,
                    output_complete = ?
                WHERE run_id = ?
                """,
                (
                    serialize_audit_model(finalization),
                    finalization.outcome.value,
                    _iso(updated.updated_at),
                    updated.revision,
                    output.stdout_sha256,
                    output.stderr_sha256,
                    output.stdout_bytes,
                    output.stderr_bytes,
                    output.stdout_preview,
                    output.stderr_preview,
                    int(output.stdout_truncated),
                    int(output.stderr_truncated),
                    int(output.complete),
                    finalization.run_id,
                ),
            )
            return updated

        return self._write("finalize_audit_run", transition)

    def get_run(self, run_id: str) -> AuditRunSnapshot:
        _required_text(run_id, "run_id")
        try:
            with self._connection() as connection:
                row = self._run_row(connection, run_id)
                return self._snapshot_from_row(connection, row)
        except AuditPersistenceError:
            raise
        except (ExecutionSerializationError, TypeError, ValueError) as error:
            raise AuditPersistenceError(
                f"stored audit run is invalid: {error}",
                operation="read_audit_run",
            ) from error
        except sqlite3.Error as error:
            raise AuditPersistenceError(
                f"could not read audit run: {error}",
                operation="read_audit_run",
            ) from error

    def get_events(self, run_id: str) -> tuple[AuditEvent, ...]:
        _required_text(run_id, "run_id")
        try:
            with self._connection() as connection:
                self._run_row(connection, run_id)
                rows = connection.execute(
                    """
                    SELECT *
                    FROM audit_events
                    WHERE run_id = ?
                    ORDER BY sequence
                    """,
                    (run_id,),
                ).fetchall()
                return tuple(self._event_from_row(row) for row in rows)
        except AuditPersistenceError:
            raise
        except (
            ExecutionSerializationError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise AuditPersistenceError(
                f"stored audit events are invalid: {error}",
                operation="read_audit_events",
            ) from error
        except sqlite3.Error as error:
            raise AuditPersistenceError(
                f"could not read audit events: {error}",
                operation="read_audit_events",
            ) from error

    def list_runs(
        self,
        *,
        workspace_id: str | None = None,
        limit: int = 200,
    ) -> tuple[AuditRunSnapshot, ...]:
        if workspace_id is not None:
            _required_text(workspace_id, "workspace_id")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        try:
            with self._connection() as connection:
                if workspace_id is None:
                    rows = connection.execute(
                        """
                        SELECT *
                        FROM audit_runs
                        ORDER BY updated_at DESC, run_id DESC
                        LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT *
                        FROM audit_runs
                        WHERE workspace_id = ?
                        ORDER BY updated_at DESC, run_id DESC
                        LIMIT ?
                        """,
                        (workspace_id, limit),
                    ).fetchall()
                return tuple(
                    self._snapshot_from_row(connection, row) for row in rows
                )
        except AuditPersistenceError:
            raise
        except (
            ExecutionSerializationError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise AuditPersistenceError(
                f"stored audit runs are invalid: {error}",
                operation="list_audit_runs",
            ) from error
        except sqlite3.Error as error:
            raise AuditPersistenceError(
                f"could not list audit runs: {error}",
                operation="list_audit_runs",
            ) from error

    def apply_retention(self, policy: RetentionPolicy) -> RetentionReport:
        if not isinstance(policy, RetentionPolicy):
            raise TypeError("policy must be a RetentionPolicy")
        if not policy.keep_nonterminal:
            raise ValueError("operational retention must preserve nonterminal runs")

        with self._access_lock:
            temporary: Path | None = None
            try:
                with self._connection() as source:
                    rows = source.execute(
                        """
                        SELECT run_id, updated_at, phase = 'terminal' AS terminal
                        FROM audit_runs
                        ORDER BY updated_at, run_id
                        """
                    ).fetchall()
                    deletable, report = plan_retention(
                        tuple(
                            (
                                str(row["run_id"]),
                                _parse_datetime(str(row["updated_at"])),
                                bool(row["terminal"]),
                            )
                            for row in rows
                        ),
                        policy,
                        now=self._now(),
                    )
                    if not deletable:
                        return report
                    source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    deleted = frozenset(deletable)
                    retained = tuple(
                        str(row["run_id"])
                        for row in rows
                        if str(row["run_id"]) not in deleted
                    )
                    temporary = self._build_retained_database(source, retained)
                self._install_retained_database(temporary)
                return report
            except (AuditPersistenceError, ValueError):
                raise
            except (
                sqlite3.Error,
                OSError,
                AuditUnavailableError,
                ExecutionSerializationError,
            ) as error:
                raise AuditPersistenceError(
                    f"audit retention failed: {error}",
                    operation="apply_audit_retention",
                ) from error
            finally:
                if temporary is not None:
                    self._remove_database_files(temporary)

    def claim_nonterminal(
        self,
        owner: str,
        *,
        lease_seconds: float = 30.0,
        limit: int = 100,
    ) -> tuple[AuditRunSnapshot, ...]:
        _required_text(owner, "owner")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        now = self._now()
        lease_until = now + timedelta(seconds=lease_seconds)

        def claim(connection: sqlite3.Connection) -> tuple[AuditRunSnapshot, ...]:
            rows = connection.execute(
                """
                SELECT *
                FROM audit_runs
                WHERE phase != 'terminal'
                  AND (
                    recovery_owner IS NULL
                    OR recovery_lease_until <= ?
                  )
                ORDER BY created_at, run_id
                LIMIT ?
                """,
                (_iso(now), limit),
            ).fetchall()
            claimed: list[AuditRunSnapshot] = []
            for row in rows:
                cursor = connection.execute(
                    """
                    UPDATE audit_runs
                    SET recovery_owner = ?, recovery_lease_until = ?
                    WHERE run_id = ? AND phase != 'terminal'
                      AND (
                        recovery_owner IS NULL
                        OR recovery_lease_until <= ?
                      )
                    """,
                    (owner, _iso(lease_until), row["run_id"], _iso(now)),
                )
                if cursor.rowcount != 1:
                    continue
                claimed_row = self._run_row(connection, str(row["run_id"]))
                claimed.append(self._snapshot_from_row(connection, claimed_row))
            return tuple(claimed)

        return self._write("claim_audit_recovery", claim)

    def _initialize(self) -> None:
        self._permissions.prepare(self.database_path)
        try:
            with self._connection() as connection:
                initialize_schema(connection)
            self._permissions.secure_sidecars(self.database_path)
        except AuditUnavailableError:
            raise
        except sqlite3.Error as error:
            raise AuditUnavailableError(
                f"could not open the audit database: {error}",
                operation="initialize_audit_store",
            ) from error

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._access_lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(
                    self.database_path,
                    isolation_level=None,
                    timeout=5.0,
                )
                configure_connection(connection)
                yield connection
            finally:
                if connection is not None:
                    connection.close()

    def _build_retained_database(
        self,
        source: sqlite3.Connection,
        retained: tuple[str, ...],
    ) -> Path:
        temporary = self.database_path.with_name(
            f".{self.database_path.name}.retention-{uuid.uuid4().hex}"
        )
        complete = False
        try:
            self._permissions.prepare(temporary)
            destination = sqlite3.connect(
                temporary,
                isolation_level=None,
                timeout=5.0,
            )
            try:
                configure_connection(destination)
                initialize_schema(destination)
                destination.execute(
                    "DROP TRIGGER audit_resources_not_after_terminal"
                )
                destination.execute("BEGIN IMMEDIATE")
                try:
                    self._copy_retained_rows(
                        source,
                        destination,
                        "audit_runs",
                        retained,
                    )
                    self._copy_retained_rows(
                        source,
                        destination,
                        "audit_resources",
                        retained,
                    )
                    self._copy_retained_rows(
                        source,
                        destination,
                        "audit_events",
                        retained,
                    )
                    destination.execute(
                        """
                        CREATE TRIGGER audit_resources_not_after_terminal
                        BEFORE INSERT ON audit_resources
                        WHEN (
                            SELECT phase FROM audit_runs WHERE run_id = NEW.run_id
                        ) = 'terminal'
                        BEGIN
                            SELECT RAISE(
                                ABORT,
                                'cannot attach a resource to a terminal run'
                            );
                        END
                        """
                    )
                    destination.commit()
                except Exception:
                    if destination.in_transaction:
                        destination.rollback()
                    raise
                verify_schema(destination)
                if destination.execute("PRAGMA foreign_key_check").fetchall():
                    raise AuditPersistenceError(
                        "retained audit database has invalid foreign keys",
                        operation="apply_audit_retention",
                    )
                destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                destination.close()
            self._permissions.secure_sidecars(temporary)
            complete = True
            return temporary
        finally:
            if not complete:
                self._remove_database_files(temporary)

    def _install_retained_database(self, temporary: Path) -> None:
        self._remove_sidecars(self.database_path)
        os.replace(temporary, self.database_path)
        self._permissions.prepare(self.database_path)
        self._permissions.secure_sidecars(self.database_path)

    @staticmethod
    def _remove_sidecars(database_path: Path) -> None:
        for suffix in ("-wal", "-shm"):
            try:
                Path(f"{database_path}{suffix}").unlink()
            except FileNotFoundError:
                pass

    @classmethod
    def _remove_database_files(cls, database_path: Path) -> None:
        try:
            database_path.unlink()
        except FileNotFoundError:
            pass
        cls._remove_sidecars(database_path)

    @staticmethod
    def _copy_retained_rows(
        source: sqlite3.Connection,
        destination: sqlite3.Connection,
        table: str,
        retained: tuple[str, ...],
    ) -> None:
        if table not in {"audit_runs", "audit_resources", "audit_events"}:
            raise ValueError("unsupported audit table")
        if not retained:
            return
        columns = tuple(
            str(row["name"])
            for row in source.execute(f"PRAGMA table_info({table})").fetchall()
        )
        column_sql = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        run_placeholders = ", ".join("?" for _ in retained)
        rows = source.execute(
            f"SELECT {column_sql} FROM {table} "
            f"WHERE run_id IN ({run_placeholders})"
            + (" ORDER BY run_id, sequence" if table == "audit_events" else ""),
            retained,
        ).fetchall()
        destination.executemany(
            f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
            (tuple(row[column] for column in columns) for row in rows),
        )


    def _write(
        self,
        operation: str,
        callback: Callable[[sqlite3.Connection], object],
    ):
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    result = callback(connection)
                    connection.commit()
                except Exception:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
            self._permissions.secure_sidecars(self.database_path)
            return result
        except AuditPersistenceError:
            raise
        except (
            sqlite3.Error,
            AuditUnavailableError,
            ExecutionSerializationError,
        ) as error:
            raise AuditPersistenceError(
                f"durable audit write failed: {error}",
                operation=operation,
            ) from error

    def _allocate_event(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        event_type: AuditEventType,
        *,
        occurred_at: datetime | None = None,
        message: str | None = None,
        metadata: Metadata = (),
        terminal: bool = False,
    ) -> AuditEvent:
        sequence = int(row["next_event_sequence"])
        phase = AuditRunPhase.TERMINAL if terminal else AuditRunPhase(str(row["phase"]))
        event = AuditEvent(
            event_id=self._new_event_id(),
            run_id=str(row["run_id"]),
            sequence=sequence,
            occurred_at=occurred_at or self._now(),
            phase=phase,
            event_type=event_type,
            message=message,
            metadata=metadata,
            terminal=terminal,
        )
        self._insert_event(connection, event)
        connection.execute(
            """
            UPDATE audit_runs
            SET next_event_sequence = next_event_sequence + 1
            WHERE run_id = ?
            """,
            (event.run_id,),
        )
        return event

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        event: AuditEvent,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                event_id, run_id, sequence, occurred_at, phase, event_type,
                message, metadata_json, terminal
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.run_id,
                event.sequence,
                _iso(event.occurred_at),
                event.phase.value,
                event.event_type.value,
                event.message,
                _encode_metadata(event.metadata),
                int(event.terminal),
            ),
        )

    @staticmethod
    def _run_row(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM audit_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise AuditPersistenceError(
                f"audit run not found: {run_id}",
                operation="read_audit_run",
            )
        return row

    @staticmethod
    def _resource_for(
        connection: sqlite3.Connection,
        run_id: str,
    ) -> BackendResourceIdentifier | None:
        row = connection.execute(
            "SELECT resource_json FROM audit_resources WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        model = deserialize_audit_model(str(row["resource_json"]))
        if not isinstance(model, BackendResourceIdentifier):
            raise AuditPersistenceError(
                "audit resource has an invalid serialized type",
                operation="read_audit_resource",
            )
        return model

    def _snapshot_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> AuditRunSnapshot:
        admission = AuditRunAdmission(
            run_id=str(row["run_id"]),
            execution_id=str(row["execution_id"]),
            tool_call_id=str(row["tool_call_id"]),
            session_id=str(row["session_id"]),
            turn_id=str(row["turn_id"]),
            workspace_id=str(row["workspace_id"]),
            request_sha256=str(row["request_sha256"]),
            request_summary=_decode_metadata(str(row["request_summary_json"])),
            created_at=_parse_datetime(str(row["created_at"])),
        )
        return AuditRunSnapshot(
            admission=admission,
            record=self._record_from_row(row),
            resource=self._resource_for(connection, admission.run_id),
            recovery_owner=(
                str(row["recovery_owner"])
                if row["recovery_owner"] is not None
                else None
            ),
            recovery_lease_until=(
                _parse_datetime(str(row["recovery_lease_until"]))
                if row["recovery_lease_until"] is not None
                else None
            ),
        )

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> AuditRunRecord:
        start: AuditRunStart | None = None
        if row["start_json"] is not None:
            decoded = deserialize_audit_model(str(row["start_json"]))
            if not isinstance(decoded, AuditRunStart):
                raise AuditPersistenceError(
                    "audit start has an invalid serialized type",
                    operation="read_audit_run",
                )
            start = decoded

        finalization: AuditFinalization | None = None
        if row["finalization_json"] is not None:
            decoded = deserialize_audit_model(str(row["finalization_json"]))
            if not isinstance(decoded, AuditFinalization):
                raise AuditPersistenceError(
                    "audit finalization has an invalid serialized type",
                    operation="read_audit_run",
                )
            finalization = decoded

        return AuditRunRecord(
            run_id=str(row["run_id"]),
            created_at=_parse_datetime(str(row["created_at"])),
            updated_at=_parse_datetime(str(row["updated_at"])),
            phase=AuditRunPhase(str(row["phase"])),
            start=start,
            finalization=finalization,
            revision=int(row["revision"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> AuditEvent:
        return AuditEvent(
            event_id=str(row["event_id"]),
            run_id=str(row["run_id"]),
            sequence=int(row["sequence"]),
            occurred_at=_parse_datetime(str(row["occurred_at"])),
            phase=AuditRunPhase(str(row["phase"])),
            event_type=AuditEventType(str(row["event_type"])),
            message=str(row["message"]) if row["message"] is not None else None,
            metadata=_decode_metadata(str(row["metadata_json"])),
            terminal=bool(row["terminal"]),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise AuditPersistenceError(
                "audit clock must return a UTC datetime",
                operation="audit_clock",
            )
        return value

    def _new_event_id(self) -> str:
        return _required_text(self._event_id_factory(), "event_id")


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("audit timestamps must be UTC datetimes")
    return value.isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _encode_metadata(metadata: Metadata) -> str:
    return canonical_json([[key, value] for key, value in metadata])


def _decode_metadata(payload: str) -> Metadata:
    value = json.loads(payload)
    if not isinstance(value, list):
        raise AuditPersistenceError(
            "stored audit metadata is not an array",
            operation="read_audit_metadata",
        )
    result: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise AuditPersistenceError(
                "stored audit metadata contains an invalid item",
                operation="read_audit_metadata",
            )
        result.append((item[0], item[1]))
    return tuple(result)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value
