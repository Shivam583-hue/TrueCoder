from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from truecoder.execution.audit.models import (
    AuditRunPhase,
    BackendResourceIdentifier,
    TerminalOutcome,
)
from truecoder.execution.audit.recovery import (
    AuditRecoveryCoordinator,
    RecoveryDisposition,
)
from truecoder.execution.audit.service import AuditService
from truecoder.execution.audit.store import SQLiteAuditStore
from truecoder.execution.errors import AuditUnavailableError
from truecoder.execution.models import (
    ExecutionContext,
    ExecutionLimits,
    ExecutionRequest,
)

NOW = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)


def context(root: Path) -> ExecutionContext:
    return ExecutionContext(
        execution_id="exec-01",
        tool_call_id="call-01",
        session_id="session-01",
        turn_id="turn-01",
        workspace_id="workspace-01",
        project_root=root.resolve(),
        launched_at_utc=NOW,
    )


def request(root: Path) -> ExecutionRequest:
    return ExecutionRequest(
        mode="exec",
        argv=("python", "-V"),
        script=None,
        working_directory=root.resolve(),
        limits=ExecutionLimits(
            timeout_seconds=30,
            max_output_bytes=4096,
            max_return_bytes=1024,
            memory_bytes=1024 * 1024,
            cpu_seconds=10,
            max_processes=8,
            termination_grace_seconds=1,
        ),
        network_access=False,
        filesystem_mode="workspace-read",
        backend="local",
        shell_kind="auto",
        environment=(("SAFE_NAME", "value-that-must-not-be-audited"),),
        require_cancellation=True,
    )


def resource() -> BackendResourceIdentifier:
    return BackendResourceIdentifier(
        version=1,
        backend="posix",
        resource_kind="process_group",
        resource_id="7331",
        ownership_token="ownership-01",
        host_id="host-01",
        created_at_utc=NOW,
        native_details=(("pgid", "7331"),),
    )


class _UnavailableStore:
    def create_pending(self, _admission):
        raise AuditUnavailableError(
            "database unavailable",
            operation="create_pending_audit",
        )


class _RecoveryHandler:
    def __init__(self, disposition):
        self.disposition = disposition
        self.resources = []

    async def recover(self, native_resource):
        self.resources.append(native_resource)
        return self.disposition


class _FailingRecoveryHandler:
    async def recover(self, _native_resource):
        raise OSError("process lookup failed")


async def _run_inline(function, /, *args, **kwargs):
    return function(*args, **kwargs)


class AuditServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch(
            "truecoder.execution.audit.service.asyncio.to_thread",
            side_effect=_run_inline,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_admission_fails_closed_when_durable_storage_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = AuditService(  # type: ignore[arg-type]
                _UnavailableStore(),
                run_id_factory=lambda: "run-01",
                clock=lambda: NOW,
            )

            with self.assertRaises(AuditUnavailableError):
                await audit.admit(context(root), request(root))

    async def test_admission_stores_request_evidence_without_environment_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteAuditStore(root / "audit.sqlite3", clock=lambda: NOW)
            audit = AuditService(
                store,
                run_id_factory=lambda: "run-01",
                clock=lambda: NOW,
            )

            handle = await audit.admit(context(root), request(root))
            snapshot = store.get_run(handle.run_id)
            summary = dict(snapshot.admission.request_summary)

            self.assertEqual(summary["command"], "python -V")
            self.assertEqual(summary["environment_names"], "SAFE_NAME")
            self.assertNotIn(
                "value-that-must-not-be-audited",
                repr(snapshot.admission),
            )
            self.assertEqual(len(snapshot.admission.request_sha256), 64)

    async def test_handle_identity_must_match_before_lifecycle_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteAuditStore(root / "audit.sqlite3", clock=lambda: NOW)
            audit = AuditService(
                store,
                run_id_factory=lambda: "run-01",
                clock=lambda: NOW,
            )
            handle = await audit.admit(context(root), request(root))
            wrong_handle = type(handle)(handle.run_id, "wrong-execution")

            with self.assertRaises(ValueError):
                await audit.attach_resource(wrong_handle, resource())


class AuditRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch(
            "truecoder.execution.audit.service.asyncio.to_thread",
            side_effect=_run_inline,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_recovery_passes_exact_resource_to_backend_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteAuditStore(root / "audit.sqlite3", clock=lambda: NOW)
            audit = AuditService(
                store,
                run_id_factory=lambda: "run-01",
                clock=lambda: NOW,
            )
            handle = await audit.admit(context(root), request(root))
            native_resource = resource()
            await audit.attach_resource(handle, native_resource)
            await audit.mark_running(handle, native_resource)
            handler = _RecoveryHandler(RecoveryDisposition.TERMINATED)
            coordinator = AuditRecoveryCoordinator(audit, {"posix": handler})

            records = await coordinator.recover_startup("startup-01")

            self.assertEqual(handler.resources, [native_resource])
            self.assertEqual(len(records), 1)
            self.assertEqual(
                records[0].outcome,
                TerminalOutcome.RECOVERED_TERMINATED,
            )
            self.assertEqual(
                store.get_run(handle.run_id).record.phase,
                AuditRunPhase.TERMINAL,
            )
            self.assertEqual(
                sum(event.terminal for event in store.get_events(handle.run_id)),
                1,
            )

    async def test_pending_run_without_resource_recovers_terminally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteAuditStore(root / "audit.sqlite3", clock=lambda: NOW)
            audit = AuditService(
                store,
                run_id_factory=lambda: "run-01",
                clock=lambda: NOW,
            )
            handle = await audit.admit(context(root), request(root))

            records = await AuditRecoveryCoordinator(
                audit,
                {},
            ).recover_startup("startup-01")

            self.assertEqual(
                records[0].outcome,
                TerminalOutcome.RECOVERED_NO_RESOURCE,
            )
            self.assertEqual(
                sum(event.terminal for event in store.get_events(handle.run_id)),
                1,
            )

    async def test_recovery_handler_failure_becomes_terminal_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteAuditStore(root / "audit.sqlite3", clock=lambda: NOW)
            audit = AuditService(
                store,
                run_id_factory=lambda: "run-01",
                clock=lambda: NOW,
            )
            handle = await audit.admit(context(root), request(root))
            native_resource = resource()
            await audit.attach_resource(handle, native_resource)
            await audit.mark_running(handle, native_resource)

            records = await AuditRecoveryCoordinator(
                audit,
                {"posix": _FailingRecoveryHandler()},
            ).recover_startup("startup-01")

            self.assertEqual(records[0].outcome, TerminalOutcome.RECOVERY_FAILED)
            events = store.get_events(handle.run_id)
            self.assertEqual(sum(event.terminal for event in events), 1)
            self.assertTrue(events[-1].terminal)


if __name__ == "__main__":
    unittest.main()
