from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from truecoder.execution.audit.models import (
    AuditEventType,
    AuditFinalization,
    AuditRunAdmission,
    AuditRunPhase,
    AuditRunStart,
    BackendResourceIdentifier,
    OutputEvidence,
    TerminalOutcome,
)
from truecoder.execution.audit.retention import RetentionPolicy
from truecoder.execution.audit.store import SQLiteAuditStore
from truecoder.execution.errors import (
    AuditPersistenceError,
    AuditUnavailableError,
)

NOW = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)


def admission(
    run_id: str = "run-01",
    *,
    workspace_id: str = "workspace-01",
    created_at: datetime = NOW,
) -> AuditRunAdmission:
    return AuditRunAdmission(
        run_id=run_id,
        execution_id=f"exec-{run_id}",
        tool_call_id="call-01",
        session_id="session-01",
        turn_id="turn-01",
        workspace_id=workspace_id,
        request_sha256="1" * 64,
        request_summary=(("command", "python -V"),),
        created_at=created_at,
    )


def resource() -> BackendResourceIdentifier:
    return BackendResourceIdentifier(
        version=1,
        backend="posix",
        resource_kind="process_group",
        resource_id="9451",
        ownership_token="token-01",
        host_id="host-01",
        created_at_utc=NOW,
        native_details=(("pgid", "9451"),),
    )


class SQLiteAuditStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "audit.sqlite3"
        self.store = SQLiteAuditStore(self.path, clock=lambda: NOW)

    def tearDown(self):
        self.directory.cleanup()

    def test_pending_record_and_creation_event_survive_reopen(self):
        handle = self.store.create_pending(admission())
        reopened = SQLiteAuditStore(self.path, clock=lambda: NOW)

        snapshot = reopened.get_run(handle.run_id)
        events = reopened.get_events(handle.run_id)

        self.assertEqual(snapshot.record.phase, AuditRunPhase.PENDING)
        self.assertEqual(snapshot.admission.execution_id, handle.execution_id)
        self.assertEqual([event.sequence for event in events], [0])
        self.assertEqual(events[0].event_type, AuditEventType.RUN_CREATED)

    def test_resource_start_and_terminal_finalization_are_durable(self):
        self.store.create_pending(admission())
        native_resource = resource()
        self.store.attach_resource("run-01", native_resource, attached_at=NOW)
        self.store.mark_running(
            AuditRunStart(
                run_id="run-01",
                started_at=NOW,
                resource=native_resource,
            )
        )
        finalization = AuditFinalization(
            run_id="run-01",
            finalized_at=NOW,
            outcome=TerminalOutcome.COMPLETED,
            command_started=True,
            exit_code=0,
            output=OutputEvidence(
                stdout_sha256="2" * 64,
                stdout_bytes=2,
                stdout_preview="ok",
            ),
            resource=native_resource,
        )

        first = self.store.finalize(finalization)
        second = self.store.finalize(finalization)
        reopened = SQLiteAuditStore(self.path, clock=lambda: NOW)
        snapshot = reopened.get_run("run-01")
        events = reopened.get_events("run-01")

        self.assertEqual(first, second)
        self.assertEqual(snapshot.record.outcome, TerminalOutcome.COMPLETED)
        self.assertEqual(snapshot.resource, native_resource)
        self.assertEqual(sum(event.terminal for event in events), 1)
        self.assertEqual(events[-1].event_type, AuditEventType.RUN_FINALIZED)
        with self.assertRaises(AuditPersistenceError):
            reopened.append_event("run-01", AuditEventType.CLEANUP_COMPLETED)

    def test_conflicting_terminal_state_is_rejected(self):
        self.store.create_pending(admission())
        self.store.finalize(
            AuditFinalization(
                run_id="run-01",
                finalized_at=NOW,
                outcome=TerminalOutcome.POLICY_DENIED,
                command_started=False,
            )
        )
        with self.assertRaises(AuditPersistenceError):
            self.store.finalize(
                AuditFinalization(
                    run_id="run-01",
                    finalized_at=NOW,
                    outcome=TerminalOutcome.APPROVAL_REJECTED,
                    command_started=False,
                )
            )

    def test_resource_and_event_commit_atomically(self):
        ids = iter(("duplicate-event", "duplicate-event"))
        store = SQLiteAuditStore(
            self.path,
            clock=lambda: NOW,
            event_id_factory=lambda: next(ids),
        )
        store.create_pending(admission())

        with self.assertRaises(AuditPersistenceError):
            store.attach_resource("run-01", resource(), attached_at=NOW)

        self.assertIsNone(store.get_run("run-01").resource)
        self.assertEqual(len(store.get_events("run-01")), 1)

    def test_recovery_claims_are_leased_and_reclaimable(self):
        self.store.create_pending(admission("run-01"))
        self.store.create_pending(admission("run-02"))

        first = self.store.claim_nonterminal("owner-one", lease_seconds=10)
        blocked = self.store.claim_nonterminal("owner-two", lease_seconds=10)
        later = SQLiteAuditStore(
            self.path,
            clock=lambda: NOW + timedelta(seconds=11),
        )
        reclaimed = later.claim_nonterminal("owner-two", lease_seconds=10)

        self.assertEqual(len(first), 2)
        self.assertEqual(blocked, ())
        self.assertEqual(len(reclaimed), 2)
        self.assertTrue(all(item.recovery_owner == "owner-two" for item in reclaimed))

    def test_list_runs_is_bounded_newest_first_and_workspace_scoped(self):
        self.store.create_pending(
            admission(
                "old",
                created_at=NOW - timedelta(minutes=2),
            )
        )
        self.store.create_pending(
            admission(
                "other-workspace",
                workspace_id="workspace-02",
                created_at=NOW - timedelta(minutes=1),
            )
        )
        self.store.create_pending(admission("new"))

        all_runs = self.store.list_runs(limit=2)
        workspace_runs = self.store.list_runs(
            workspace_id="workspace-01",
            limit=10,
        )

        self.assertEqual(
            [snapshot.record.run_id for snapshot in all_runs],
            ["new", "other-workspace"],
        )
        self.assertEqual(
            [snapshot.record.run_id for snapshot in workspace_runs],
            ["new", "old"],
        )

    def test_list_runs_rejects_unbounded_limits(self):
        with self.assertRaises(ValueError):
            self.store.list_runs(limit=501)

    def test_retention_atomically_rebuilds_only_terminal_expired_evidence(self):
        old_time = NOW - timedelta(days=90)
        self.store.create_pending(admission("old", created_at=old_time))
        self.store.finalize(
            AuditFinalization(
                run_id="old",
                finalized_at=old_time,
                outcome=TerminalOutcome.POLICY_DENIED,
                command_started=False,
            )
        )
        self.store.create_pending(admission("recent"))
        self.store.create_pending(
            admission("old-nonterminal", created_at=old_time)
        )

        report = self.store.apply_retention(RetentionPolicy(days=30))

        self.assertEqual(report.deleted, 1)
        with self.assertRaises(AuditPersistenceError):
            self.store.get_run("old")
        self.assertEqual(self.store.get_run("recent").record.run_id, "recent")
        self.assertEqual(
            self.store.get_run("old-nonterminal").record.phase,
            AuditRunPhase.PENDING,
        )
        reopened = SQLiteAuditStore(self.path, clock=lambda: NOW)
        self.assertEqual(len(reopened.get_events("recent")), 1)
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM audit_runs")
        finally:
            connection.close()

    def test_operational_retention_never_accepts_nonterminal_deletion(self):
        with self.assertRaises(ValueError):
            self.store.apply_retention(
                RetentionPolicy(days=30, keep_nonterminal=False)
            )

    def test_sqlite_triggers_prevent_mutating_evidence(self):
        self.store.create_pending(admission())
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE audit_events SET message = 'changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM audit_runs")
        finally:
            connection.close()

    def test_terminal_event_rolls_back_if_run_finalization_cannot_commit(self):
        self.store.create_pending(admission())
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """
                CREATE TRIGGER reject_test_finalization
                BEFORE UPDATE OF finalization_json ON audit_runs
                WHEN NEW.finalization_json IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'simulated finalization failure');
                END
                """
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(AuditPersistenceError):
            self.store.finalize(
                AuditFinalization(
                    run_id="run-01",
                    finalized_at=NOW,
                    outcome=TerminalOutcome.POLICY_DENIED,
                    command_started=False,
                )
            )

        snapshot = self.store.get_run("run-01")
        events = self.store.get_events("run-01")
        self.assertEqual(snapshot.record.phase, AuditRunPhase.PENDING)
        self.assertEqual(sum(event.terminal for event in events), 0)
        self.assertEqual(len(events), 1)

    def test_future_schema_version_fails_closed(self):
        future_path = Path(self.directory.name) / "future.sqlite3"
        connection = sqlite3.connect(future_path)
        try:
            connection.execute("PRAGMA user_version = 999")
        finally:
            connection.close()

        with self.assertRaises(AuditUnavailableError):
            SQLiteAuditStore(future_path, clock=lambda: NOW)


if __name__ == "__main__":
    unittest.main()
