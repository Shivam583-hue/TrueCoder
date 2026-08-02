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
from truecoder.execution.audit.store import SQLiteAuditStore
from truecoder.execution.errors import AuditPersistenceError

NOW = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)


def admission(run_id: str = "run-01") -> AuditRunAdmission:
    return AuditRunAdmission(
        run_id=run_id,
        execution_id=f"exec-{run_id}",
        tool_call_id="call-01",
        session_id="session-01",
        turn_id="turn-01",
        workspace_id="workspace-01",
        request_sha256="1" * 64,
        request_summary=(("command", "python -V"),),
        created_at=NOW,
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


if __name__ == "__main__":
    unittest.main()
