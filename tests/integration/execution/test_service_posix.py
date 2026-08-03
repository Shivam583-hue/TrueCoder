from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from truecoder.execution.audit.models import AuditRunPhase, TerminalOutcome
from truecoder.execution.audit.service import AuditService
from truecoder.execution.audit.store import SQLiteAuditStore
from truecoder.execution.backends.models import (
    BackendDescriptor,
    DiscoveredProgram,
)
from truecoder.execution.backends.posix import PosixBackend
from truecoder.execution.backends.registry import BackendRegistry
from truecoder.execution.environment import construct_environment
from truecoder.execution.errors import BackendStartError
from truecoder.execution.models import (
    BackendCapabilities,
    CapabilityRequirements,
    ExecutionContext,
    ExecutionLimits,
    ExecutionRequest,
    PolicyDecision,
    RiskLevel,
)
from truecoder.execution.preparation import PreparedExecution
from truecoder.execution.registry import ExecutionRegistry
from truecoder.execution.runner import ExecutionRunner
from truecoder.execution.service import ExecutionService

ROOT = Path.cwd().resolve()
HELPERS = ROOT / "tests" / "helpers" / "execution"
HOST_ENVIRONMENT = {"PATH": os.defpath, "LANG": "C.UTF-8"}


def descriptor() -> BackendDescriptor:
    return BackendDescriptor(
        name="posix",
        available=True,
        capabilities=BackendCapabilities(
            filesystem_isolation="unsupported",
            network_isolation="unsupported",
            memory_limits="best_effort",
            cpu_limits="best_effort",
            process_limits="best_effort",
            timeout_enforcement="enforced",
            cancellation="enforced",
            supported_execution_modes=("exec", "shell"),
            supported_filesystem_modes=("host",),
            supported_shells=("posix",),
        ),
        version="integration",
    )


def backend() -> PosixBackend:
    shell_path = shutil.which("sh")
    assert shell_path is not None
    return PosixBackend(
        descriptor(),
        shells=(
            DiscoveredProgram(
                name="sh",
                path=Path(shell_path),
                shell_kind="posix",
            ),
        ),
    )


def request(
    argv: tuple[str, ...],
    *,
    timeout_seconds: float = 20.0,
    max_output_bytes: int = 1024 * 1024,
    max_return_bytes: int = 4096,
    directory: Path = ROOT,
) -> ExecutionRequest:
    return ExecutionRequest(
        mode="exec",
        argv=argv,
        script=None,
        working_directory=directory,
        limits=ExecutionLimits(
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            max_return_bytes=max_return_bytes,
            termination_grace_seconds=0.05,
        ),
        network_access=True,
        filesystem_mode="host",
    )


def context(execution_id: str) -> ExecutionContext:
    return ExecutionContext(
        execution_id=execution_id,
        tool_call_id=f"call-{execution_id}",
        session_id="session-integration",
        turn_id="turn-integration",
        workspace_id="workspace-integration",
        project_root=ROOT,
        launched_at_utc=datetime.now(UTC),
    )


def prepared(execution_request: ExecutionRequest) -> PreparedExecution:
    return PreparedExecution(
        request=execution_request,
        backend=descriptor(),
        environment=construct_environment(
            platform="posix",
            inherited=HOST_ENVIRONMENT,
            requested=execution_request.environment,
        ),
        resolved_shell=None,
    )


def decision() -> PolicyDecision:
    return PolicyDecision(
        allowed=True,
        risk=RiskLevel.LOW,
        requires_approval=False,
        effective_limits=request(("true",)).limits,
        requirements=CapabilityRequirements(),
    )


@unittest.skipUnless(os.name == "posix", "requires POSIX process semantics")
class PosixServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.database = Path(self._directory.name) / "audit.sqlite3"
        self.store = SQLiteAuditStore(self.database)
        self.audit = AuditService(self.store)
        self.registry = ExecutionRegistry()

    def service(self, chosen: PosixBackend | None = None) -> ExecutionService:
        runner = ExecutionRunner(
            self.audit,
            BackendRegistry((chosen or backend(),)),
            registry=self.registry,
            safety_deadline_seconds=5.0,
        )
        return ExecutionService(self.registry, runner=runner, audit=self.audit)

    async def test_a_successful_command_runs_end_to_end(self):
        service = self.service()
        launch = request(
            (
                sys.executable,
                str(HELPERS / "emit_output.py"),
                "--stdout",
                "hello",
                "--stderr",
                "warning",
                "--exit-code",
                "0",
            )
        )

        result = await service.run_prepared(
            prepared(launch),
            decision(),
            context("exec-integration-ok"),
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "hello")
        self.assertEqual(result.stderr, "warning")
        self.assertEqual(result.backend, "posix")
        self.assertGreater(result.duration_seconds, 0.0)
        self.assertEqual(await self.registry.active_execution_ids(), ())

        snapshot = await self.audit.get_run(result.audit_id)
        self.assertIs(snapshot.record.phase, AuditRunPhase.TERMINAL)
        assert snapshot.record.finalization is not None
        self.assertIs(
            snapshot.record.finalization.outcome,
            TerminalOutcome.COMPLETED,
        )

    async def test_a_nonzero_command_is_normalized_to_failed(self):
        service = self.service()
        launch = request(
            (
                sys.executable,
                str(HELPERS / "emit_output.py"),
                "--stdout",
                "out",
                "--exit-code",
                "3",
            )
        )

        result = await service.run_prepared(
            prepared(launch),
            decision(),
            context("exec-integration-fail"),
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, 3)
        snapshot = await self.audit.get_run(result.audit_id)
        assert snapshot.record.finalization is not None
        self.assertIs(snapshot.record.finalization.outcome, TerminalOutcome.FAILED)

    async def test_a_real_timeout_terminates_the_process_group(self):
        service = self.service()
        launch = request(
            (sys.executable, str(HELPERS / "ignore_term.py")),
            timeout_seconds=0.3,
        )

        result = await service.run_prepared(
            prepared(launch),
            decision(),
            context("exec-integration-timeout"),
        )

        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.termination_reason, "timeout")
        self.assertIsNone(result.exit_code)
        pid = int(result.stdout.strip() or 0)
        if pid:
            self.assertFalse(_process_alive(pid))
        self.assertEqual(await self.registry.active_execution_ids(), ())

    async def test_a_real_output_limit_bounds_returned_output(self):
        service = self.service()
        launch = request(
            (
                sys.executable,
                "-c",
                "print('x' * 100000)",
            ),
            max_output_bytes=1024,
            max_return_bytes=512,
        )

        result = await service.run_prepared(
            prepared(launch),
            decision(),
            context("exec-integration-limit"),
        )

        self.assertEqual(result.status, "limit_exceeded")
        self.assertEqual(result.termination_reason, "output_limit")
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 512)
        self.assertTrue(result.stdout_truncated)

    async def test_audit_attachment_failure_keeps_the_launch_gate_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            self.store = _RefusingAttachStore(self.database)
            self.audit = AuditService(self.store)
            service = self.service()
            launch = request(
                (
                    sys.executable,
                    str(HELPERS / "write_marker.py"),
                    str(marker),
                ),
                directory=Path(directory),
            )

            with self.assertRaises(BackendStartError):
                await service.run_prepared(
                    prepared(launch),
                    decision(),
                    context("exec-integration-gate"),
                )

            self.assertFalse(marker.exists())
            self.assertEqual(await self.registry.active_execution_ids(), ())

    async def test_a_reopened_database_holds_one_terminal_run(self):
        service = self.service()
        launch = request((sys.executable, "-c", "print('done')"))

        result = await service.run_prepared(
            prepared(launch),
            decision(),
            context("exec-integration-reopen"),
        )

        reopened = SQLiteAuditStore(self.database)
        snapshot = reopened.get_run(result.audit_id)
        events = reopened.get_events(result.audit_id)

        self.assertIs(snapshot.record.phase, AuditRunPhase.TERMINAL)
        self.assertEqual(
            tuple(event.sequence for event in events),
            tuple(range(len(events))),
        )
        self.assertIn(
            "backend_starting",
            tuple(event.event_type.value for event in events),
        )


class _RefusingAttachStore(SQLiteAuditStore):
    def attach_resource(self, run_id, resource, **kwargs):
        raise RuntimeError("durable attachment refused")


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
