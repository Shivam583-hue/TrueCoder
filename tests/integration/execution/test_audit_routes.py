from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

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
from truecoder.execution.audit.recovery import (
    AuditRecoveryCoordinator,
    RecoveryDisposition,
)
from truecoder.execution.audit.service import AuditService
from truecoder.execution.audit.store import SQLiteAuditStore

NOW = datetime(2026, 8, 2, 11, 0, tzinfo=UTC)


def admission(run_id: str) -> AuditRunAdmission:
    return AuditRunAdmission(
        run_id=run_id,
        execution_id=f"exec-{run_id}",
        tool_call_id="call-01",
        session_id="session-01",
        turn_id="turn-01",
        workspace_id="workspace-01",
        request_sha256="3" * 64,
        request_summary=(("command", "python task.py"),),
        created_at=NOW,
    )


def resource(run_id: str) -> BackendResourceIdentifier:
    return BackendResourceIdentifier(
        version=1,
        backend="posix",
        resource_kind="process_group",
        resource_id=f"pgid-{run_id}",
        ownership_token=f"owner-{run_id}",
        host_id="host-01",
        created_at_utc=NOW,
        native_details=(("pgid", f"pgid-{run_id}"),),
    )


def finalization(
    run_id: str,
    outcome: TerminalOutcome,
    *,
    native_resource: BackendResourceIdentifier | None = None,
) -> AuditFinalization:
    if outcome in {
        TerminalOutcome.POLICY_DENIED,
        TerminalOutcome.APPROVAL_REJECTED,
        TerminalOutcome.FAILED_TO_START,
    }:
        return AuditFinalization(
            run_id=run_id,
            finalized_at=NOW,
            outcome=outcome,
            command_started=False,
        )
    exit_code = {
        TerminalOutcome.COMPLETED: 0,
        TerminalOutcome.FAILED: 1,
        TerminalOutcome.CLEANUP_FAILED: 1,
    }.get(outcome)
    return AuditFinalization(
        run_id=run_id,
        finalized_at=NOW,
        outcome=outcome,
        command_started=True,
        exit_code=exit_code,
        output=OutputEvidence(
            stdout_sha256="4" * 64,
            stderr_sha256="5" * 64,
            stdout_bytes=2,
            stderr_bytes=5,
            stdout_preview="ok",
            stderr_preview="error",
        ),
        resource=native_resource,
        underlying_outcome=(
            TerminalOutcome.FAILED
            if outcome is TerminalOutcome.CLEANUP_FAILED
            else None
        ),
        detail=(
            "backend cleanup could not be guaranteed"
            if outcome is TerminalOutcome.CLEANUP_FAILED
            else None
        ),
    )


class DurableAuditRouteTests(unittest.TestCase):
    def test_every_normal_route_has_exactly_one_durable_terminal_state(self):
        routes = (
            (
                "policy-denial",
                TerminalOutcome.POLICY_DENIED,
                AuditEventType.POLICY_DENIED,
                False,
            ),
            (
                "approval-rejection",
                TerminalOutcome.APPROVAL_REJECTED,
                AuditEventType.APPROVAL_REJECTED,
                False,
            ),
            (
                "failed-start",
                TerminalOutcome.FAILED_TO_START,
                AuditEventType.BACKEND_STARTING,
                False,
            ),
            (
                "timeout",
                TerminalOutcome.TIMED_OUT,
                AuditEventType.TIMEOUT_REACHED,
                True,
            ),
            (
                "cancellation",
                TerminalOutcome.CANCELLED,
                AuditEventType.CANCELLATION_REQUESTED,
                True,
            ),
            (
                "limit",
                TerminalOutcome.LIMIT_EXCEEDED,
                AuditEventType.LIMIT_REACHED,
                True,
            ),
            (
                "cleanup-failure",
                TerminalOutcome.CLEANUP_FAILED,
                AuditEventType.CLEANUP_FAILED,
                True,
            ),
            (
                "nonzero-exit",
                TerminalOutcome.FAILED,
                AuditEventType.CLEANUP_COMPLETED,
                True,
            ),
            (
                "success",
                TerminalOutcome.COMPLETED,
                AuditEventType.CLEANUP_COMPLETED,
                True,
            ),
        )

        for route, outcome, event_type, starts in routes:
            with self.subTest(route=route), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "audit.sqlite3"
                store = SQLiteAuditStore(path, clock=lambda: NOW)
                run_id = f"run-{route}"
                store.create_pending(admission(run_id))
                native_resource = None
                if starts:
                    native_resource = resource(run_id)
                    store.attach_resource(
                        run_id,
                        native_resource,
                        attached_at=NOW,
                    )
                    store.mark_running(
                        AuditRunStart(
                            run_id=run_id,
                            started_at=NOW,
                            resource=native_resource,
                        )
                    )
                store.append_event(run_id, event_type, occurred_at=NOW)
                store.finalize(
                    finalization(
                        run_id,
                        outcome,
                        native_resource=native_resource,
                    )
                )

                reopened = SQLiteAuditStore(path, clock=lambda: NOW)
                snapshot = reopened.get_run(run_id)
                events = reopened.get_events(run_id)
                self.assertEqual(snapshot.record.phase, AuditRunPhase.TERMINAL)
                self.assertEqual(snapshot.record.outcome, outcome)
                self.assertEqual(sum(event.terminal for event in events), 1)
                self.assertTrue(events[-1].terminal)
                self.assertEqual(
                    events[-1].event_type,
                    AuditEventType.RUN_FINALIZED,
                )


class _RecoveryHandler:
    def __init__(self, disposition: RecoveryDisposition) -> None:
        self.disposition = disposition
        self.seen: list[BackendResourceIdentifier] = []

    async def recover(
        self,
        native_resource: BackendResourceIdentifier,
    ) -> RecoveryDisposition:
        self.seen.append(native_resource)
        return self.disposition


async def _run_inline(function, /, *args, **kwargs):
    return function(*args, **kwargs)


class CrashRecoveryRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch(
            "truecoder.execution.audit.service.asyncio.to_thread",
            side_effect=_run_inline,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_crashed_running_route_is_recovered_and_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.sqlite3"
            run_id = "run-crashed"
            native_resource = resource(run_id)
            first_process = SQLiteAuditStore(path, clock=lambda: NOW)
            first_process.create_pending(admission(run_id))
            first_process.attach_resource(
                run_id,
                native_resource,
                attached_at=NOW,
            )
            first_process.mark_running(
                AuditRunStart(
                    run_id=run_id,
                    started_at=NOW,
                    resource=native_resource,
                )
            )

            startup_store = SQLiteAuditStore(path, clock=lambda: NOW)
            audit = AuditService(startup_store, clock=lambda: NOW)
            handler = _RecoveryHandler(RecoveryDisposition.TERMINATED)
            records = await AuditRecoveryCoordinator(
                audit,
                {"posix": handler},
            ).recover_startup("startup-01")

            reopened = SQLiteAuditStore(path, clock=lambda: NOW)
            snapshot = reopened.get_run(run_id)
            events = reopened.get_events(run_id)
            self.assertEqual(handler.seen, [native_resource])
            self.assertEqual(len(records), 1)
            self.assertEqual(
                snapshot.record.outcome,
                TerminalOutcome.RECOVERED_TERMINATED,
            )
            self.assertEqual(sum(event.terminal for event in events), 1)
            self.assertTrue(events[-1].terminal)


if __name__ == "__main__":
    unittest.main()
