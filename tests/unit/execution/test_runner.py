from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from truecoder.execution.audit.models import (
    AuditRunPhase,
    AuditRunRecord,
    BackendResourceIdentifier,
    TerminalOutcome,
)
from truecoder.execution.audit.service import AuditService
from truecoder.execution.audit.store import SQLiteAuditStore
from truecoder.execution.backends.models import (
    BackendDescriptor,
    BackendExit,
    BackendOutputChunk,
    CleanupResult,
)
from truecoder.execution.backends.registry import BackendRegistry
from truecoder.execution.cancellation import CancellationRequested
from truecoder.execution.environment import construct_environment
from truecoder.execution.errors import (
    AuditPersistenceError,
    AuditUnavailableError,
    BackendCleanupError,
    BackendStartError,
)
from truecoder.execution.lifecycle import TerminalClaim
from truecoder.execution.models import (
    BackendCapabilities,
    CapabilityRequirements,
    ExecutionContext,
    ExecutionLimits,
    ExecutionRequest,
    NativeDiagnostic,
    PolicyDecision,
    PolicyReason,
    RiskLevel,
)
from truecoder.execution.preparation import PreparedExecution
from truecoder.execution.registry import ExecutionRegistry
from truecoder.execution.results import (
    TerminalMaterial,
    build_execution_result,
    build_finalization,
    build_output_evidence,
    empty_output,
)
from truecoder.execution.runner import ExecutionRunner

NOW = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
ROOT = Path.cwd().resolve()
RUN_ID = "run_under_test"


class StoreFailure(RuntimeError):
    pass


class FailingStore:
    def __init__(self, inner: SQLiteAuditStore) -> None:
        self._inner = inner
        self.fail_on: set[str] = set()
        self.calls: list[str] = []

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail_on:
            raise StoreFailure(name)

    def create_pending(self, admission):
        self._guard("create_pending")
        return self._inner.create_pending(admission)

    def append_event(self, run_id, event_type, **kwargs):
        self._guard(f"append_event:{event_type.value}")
        return self._inner.append_event(run_id, event_type, **kwargs)

    def attach_resource(self, run_id, resource, **kwargs):
        self._guard("attach_resource")
        return self._inner.attach_resource(run_id, resource, **kwargs)

    def mark_running(self, start):
        self._guard("mark_running")
        return self._inner.mark_running(start)

    def finalize(self, finalization):
        self._guard("finalize")
        return self._inner.finalize(finalization)

    def get_run(self, run_id):
        return self._inner.get_run(run_id)

    def get_events(self, run_id):
        return self._inner.get_events(run_id)

    def claim_nonterminal(self, owner, **kwargs):
        return self._inner.claim_nonterminal(owner, **kwargs)

    def finalize_calls(self) -> int:
        return sum(1 for call in self.calls if call == "finalize")

    def event_calls(self) -> tuple[str, ...]:
        return tuple(
            call.split(":", 1)[1]
            for call in self.calls
            if call.startswith("append_event:")
        )


class FakeHandle:
    def __init__(
        self,
        context: ExecutionContext,
        *,
        exit_code: int | None = 0,
        native_reason: str | None = None,
        output: tuple[BackendOutputChunk, ...] = (),
        exit_delay: float = 0.0,
        cleanup_complete: bool = True,
    ) -> None:
        self._execution_id = context.execution_id
        self._exit_code = exit_code
        self._native_reason = native_reason
        self._output = output
        self._exit_delay = exit_delay
        self._cleanup_complete = cleanup_complete
        self._terminated = asyncio.Event()
        self.terminations: list[str] = []
        self.cleanups = 0
        self.drained = False
        self._resource = BackendResourceIdentifier(
            version=1,
            backend="posix",
            resource_kind="process_group",
            resource_id=f"pgid-{context.execution_id}",
            ownership_token=f"token-{context.execution_id}",
            host_id="host-01",
            created_at_utc=NOW,
            native_details=(("pgid", "4242"),),
        )

    @property
    def execution_id(self) -> str:
        return self._execution_id

    @property
    def resource(self) -> BackendResourceIdentifier:
        return self._resource

    def output(self) -> AsyncIterator[BackendOutputChunk]:
        async def iterate() -> AsyncIterator[BackendOutputChunk]:
            for chunk in self._output:
                yield chunk
                await asyncio.sleep(0)
            self.drained = True

        return iterate()

    async def wait(self) -> BackendExit:
        if self._exit_delay:
            try:
                await asyncio.wait_for(
                    self._terminated.wait(),
                    timeout=self._exit_delay,
                )
            except TimeoutError:
                pass
        if self.terminations:
            return BackendExit(
                exit_code=None,
                native_reason=self.terminations[0],
            )
        return BackendExit(
            exit_code=self._exit_code,
            native_reason=self._native_reason,
        )

    async def terminate(self, reason: str, grace_seconds: float) -> None:
        self.terminations.append(reason)
        self._terminated.set()

    async def cleanup(self) -> CleanupResult:
        self.cleanups += 1
        if self._cleanup_complete:
            return CleanupResult(complete=True)
        return CleanupResult(
            complete=False,
            diagnostic=NativeDiagnostic(
                code="cleanup-incomplete",
                message="cgroup remained",
                platform="posix",
            ),
        )


class FakeBackend:
    def __init__(
        self,
        descriptor: BackendDescriptor,
        *,
        handle_options: dict | None = None,
        attach_before_gate: bool = True,
    ) -> None:
        self._descriptor = descriptor
        self._handle_options = handle_options or {}
        self._attach_before_gate = attach_before_gate
        self.starts = 0
        self.gate_opened = False
        self.aborted = False
        self.handle: FakeHandle | None = None

    @property
    def descriptor(self) -> BackendDescriptor:
        return self._descriptor

    async def start(self, prepared, request, context, cancellation, register_resource):
        self.starts += 1
        handle = FakeHandle(context, **self._handle_options)
        if self._attach_before_gate:
            try:
                await register_resource(handle.resource)
            except BaseException:
                self.aborted = True
                await handle.cleanup()
                raise
        self.gate_opened = True
        self.handle = handle
        return handle


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
        version="test",
    )


def request(*, timeout_seconds: float = 30.0) -> ExecutionRequest:
    return ExecutionRequest(
        mode="exec",
        argv=("python", "-V"),
        script=None,
        working_directory=ROOT,
        limits=ExecutionLimits(
            timeout_seconds=timeout_seconds,
            max_output_bytes=64,
            max_return_bytes=64,
            termination_grace_seconds=0.01,
        ),
        network_access=True,
        filesystem_mode="host",
    )


def context(execution_id: str = "exec-runner-01") -> ExecutionContext:
    return ExecutionContext(
        execution_id=execution_id,
        tool_call_id=f"call-{execution_id}",
        session_id="session-01",
        turn_id="turn-01",
        workspace_id="workspace-01",
        project_root=ROOT,
        launched_at_utc=NOW,
    )


def prepared(execution_request: ExecutionRequest | None = None) -> PreparedExecution:
    effective = execution_request or request()
    return PreparedExecution(
        request=effective,
        backend=descriptor(),
        environment=construct_environment(
            platform="posix",
            inherited={},
            requested=effective.environment,
        ),
        resolved_shell=None,
    )


def decision(*, allowed: bool = True) -> PolicyDecision:
    return PolicyDecision(
        allowed=allowed,
        risk=RiskLevel.LOW if allowed else RiskLevel.HIGH,
        requires_approval=False,
        effective_limits=request().limits,
        requirements=CapabilityRequirements(),
        reasons=()
        if allowed
        else (
            PolicyReason(
                code="blocked-command",
                message="the command is not permitted",
                rule_id="rule-01",
            ),
        ),
    )


class RunnerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.store = FailingStore(
            SQLiteAuditStore(Path(self._directory.name) / "audit.sqlite3"),
        )
        self.audit = AuditService(
            self.store,
            run_id_factory=lambda: RUN_ID,
            clock=lambda: datetime.now(UTC),
        )
        self.registry = ExecutionRegistry()

    def build_runner(
        self,
        backend: FakeBackend | None = None,
        *,
        approval_gate=None,
    ) -> tuple[ExecutionRunner, FakeBackend]:
        chosen = backend or FakeBackend(descriptor())
        runner = ExecutionRunner(
            self.audit,
            BackendRegistry((chosen,)),
            registry=self.registry,
            approval_gate=approval_gate,
        )
        return runner, chosen

    async def newest_run(self):
        return await self.audit.get_run(RUN_ID)

    async def start_run(self, runner: ExecutionRunner, **kwargs):
        return await runner.run_prepared(
            kwargs.pop("prepared_execution", prepared()),
            kwargs.pop("policy_decision", decision()),
            kwargs.pop("execution_context", context()),
        )


class AdmissionBoundaryTests(RunnerTestCase):
    async def test_admission_failure_refuses_the_run_entirely(self):
        runner, backend = self.build_runner()
        self.store.fail_on.add("create_pending")

        with self.assertRaises(AuditUnavailableError):
            await self.start_run(runner)

        self.assertEqual(backend.starts, 0)
        self.assertEqual(self.store.finalize_calls(), 0)
        self.assertEqual(await self.registry.active_execution_ids(), ())


class PreStartEventBoundaryTests(RunnerTestCase):
    async def test_policy_event_failure_never_starts_and_finalizes_pending(self):
        runner, backend = self.build_runner()
        self.store.fail_on.add("append_event:policy_allowed")

        with self.assertRaises(AuditPersistenceError):
            await self.start_run(runner)

        self.assertEqual(backend.starts, 0)
        self.assertEqual(self.store.finalize_calls(), 1)
        snapshot = await self.newest_run()
        self.assertIs(snapshot.record.phase, AuditRunPhase.TERMINAL)
        assert snapshot.record.finalization is not None
        self.assertIs(
            snapshot.record.finalization.outcome,
            TerminalOutcome.FAILED_TO_START,
        )

    async def test_policy_event_and_finalize_failure_leaves_a_recoverable_row(self):
        runner, backend = self.build_runner()
        self.store.fail_on.update({"append_event:policy_allowed", "finalize"})

        with self.assertRaises(AuditPersistenceError):
            await self.start_run(runner)

        self.assertEqual(backend.starts, 0)
        snapshot = await self.newest_run()
        self.assertIs(snapshot.record.phase, AuditRunPhase.PENDING)
        self.assertIsNone(snapshot.record.finalization)

    async def test_approval_event_failure_fails_closed(self):
        async def approve(_prepared, _context) -> bool:
            return True

        runner, backend = self.build_runner(approval_gate=approve)
        self.store.fail_on.add("append_event:approval_requested")

        with self.assertRaises(AuditPersistenceError):
            await self.start_run(runner)

        self.assertEqual(backend.starts, 0)
        snapshot = await self.newest_run()
        self.assertIs(snapshot.record.phase, AuditRunPhase.TERMINAL)

    async def test_starting_event_failure_unregisters_and_never_calls_backend(self):
        runner, backend = self.build_runner()
        self.store.fail_on.add("append_event:backend_starting")

        result = await self.start_run(runner)

        self.assertEqual(backend.starts, 0)
        self.assertEqual(result.status, "failed_to_start")
        self.assertEqual(await self.registry.active_execution_ids(), ())
        snapshot = await self.newest_run()
        assert snapshot.record.finalization is not None
        self.assertIs(
            snapshot.record.finalization.outcome,
            TerminalOutcome.FAILED_TO_START,
        )


class DenialBoundaryTests(RunnerTestCase):
    async def test_policy_denial_finalizes_without_touching_a_backend(self):
        runner, backend = self.build_runner()

        result = await self.start_run(runner, policy_decision=decision(allowed=False))

        self.assertEqual(result.status, "denied")
        self.assertIsNone(result.backend)
        self.assertIsNone(result.exit_code)
        self.assertEqual(backend.starts, 0)
        self.assertIn("policy_denied", self.store.event_calls())
        snapshot = await self.newest_run()
        assert snapshot.record.finalization is not None
        self.assertIs(snapshot.record.finalization.outcome, TerminalOutcome.POLICY_DENIED)
        self.assertFalse(snapshot.record.finalization.command_started)
        self.assertEqual(result.audit_id, snapshot.admission.run_id)

    async def test_approval_rejection_finalizes_as_approval_rejected(self):
        async def reject(_prepared, _context) -> bool:
            return False

        runner, backend = self.build_runner(approval_gate=reject)

        result = await self.start_run(runner)

        self.assertEqual(result.status, "denied")
        self.assertEqual(backend.starts, 0)
        self.assertIn("approval_rejected", self.store.event_calls())
        snapshot = await self.newest_run()
        assert snapshot.record.finalization is not None
        self.assertIs(
            snapshot.record.finalization.outcome,
            TerminalOutcome.APPROVAL_REJECTED,
        )


class ResourceBoundaryTests(RunnerTestCase):
    async def test_attach_failure_aborts_the_launch_behind_a_closed_gate(self):
        runner, backend = self.build_runner()
        self.store.fail_on.add("attach_resource")

        with self.assertRaises(BackendStartError):
            await self.start_run(runner)

        self.assertEqual(backend.starts, 1)
        self.assertTrue(backend.aborted)
        self.assertFalse(backend.gate_opened)
        snapshot = await self.newest_run()
        self.assertIsNone(snapshot.resource)

    async def test_cancellation_during_start_stays_a_cancellation(self):
        class CancellingBackend(FakeBackend):
            async def start(
                self,
                prepared,
                request,
                context,
                cancellation,
                register_resource,
            ):
                self.starts += 1
                raise CancellationRequested("user cancelled")

        runner, backend = self.build_runner(CancellingBackend(descriptor()))

        result = await self.start_run(runner)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.termination_reason, "cancellation")
        self.assertIsNone(result.exit_code)
        self.assertEqual(backend.starts, 1)
        snapshot = await self.newest_run()
        assert snapshot.record.finalization is not None
        self.assertIs(
            snapshot.record.finalization.outcome,
            TerminalOutcome.FAILED_TO_START,
        )
        self.assertEqual(
            snapshot.record.finalization.detail,
            "cancelled_before_start",
        )

    async def test_mark_running_failure_terminates_and_cleans_the_handle(self):
        runner, backend = self.build_runner()
        self.store.fail_on.add("mark_running")

        with self.assertRaises(AuditPersistenceError):
            await self.start_run(runner)

        assert backend.handle is not None
        self.assertEqual(backend.handle.terminations, ["shutdown"])
        self.assertEqual(backend.handle.cleanups, 1)
        snapshot = await self.newest_run()
        self.assertIsNotNone(snapshot.resource)
        self.assertIsNot(snapshot.record.phase, AuditRunPhase.TERMINAL)


class RuntimeEvidenceTests(RunnerTestCase):
    async def test_runtime_event_loss_is_fatal_and_still_finalizes(self):
        backend = FakeBackend(
            descriptor(),
            handle_options={"exit_delay": 5.0},
        )
        runner, backend = self.build_runner(backend)
        self.store.fail_on.add("append_event:timeout_reached")

        with self.assertRaises(AuditPersistenceError):
            await self.start_run(
                runner,
                prepared_execution=prepared(request(timeout_seconds=0.01)),
            )

        assert backend.handle is not None
        self.assertEqual(backend.handle.terminations, ["shutdown"])
        self.assertEqual(backend.handle.cleanups, 1)
        snapshot = await self.newest_run()
        assert snapshot.record.finalization is not None
        self.assertIs(snapshot.record.finalization.outcome, TerminalOutcome.TIMED_OUT)
        self.assertEqual(
            snapshot.record.finalization.detail,
            "runtime_evidence_lost",
        )

    async def test_evidence_loss_before_any_claim_records_a_shutdown(self):
        backend = FakeBackend(descriptor(), handle_options={"exit_delay": 5.0})
        runner, backend = self.build_runner(backend)
        self.store.fail_on.add("append_event:termination_started")

        with self.assertRaises(AuditPersistenceError):
            await self.start_run(
                runner,
                prepared_execution=prepared(request(timeout_seconds=0.01)),
            )

        snapshot = await self.newest_run()
        assert snapshot.record.finalization is not None
        self.assertIs(snapshot.record.finalization.outcome, TerminalOutcome.TIMED_OUT)
        self.assertIn("timeout_reached", self.store.event_calls())


class FinalizationBoundaryTests(RunnerTestCase):
    async def test_finalize_failure_withholds_the_result(self):
        runner, _backend = self.build_runner()
        self.store.fail_on.add("finalize")

        with self.assertRaises(AuditPersistenceError):
            await self.start_run(runner)

        snapshot = await self.newest_run()
        self.assertIsNot(snapshot.record.phase, AuditRunPhase.TERMINAL)
        self.assertIsNone(snapshot.record.finalization)

    async def test_a_successful_run_finalizes_exactly_once(self):
        backend = FakeBackend(
            descriptor(),
            handle_options={
                "output": (
                    BackendOutputChunk(stream="stdout", data=b"hello"),
                    BackendOutputChunk(stream="stderr", data=b"warn"),
                ),
            },
        )
        runner, backend = self.build_runner(backend)

        result = await self.start_run(runner)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "hello")
        self.assertEqual(result.stderr, "warn")
        self.assertEqual(result.backend, "posix")
        self.assertEqual(self.store.finalize_calls(), 1)
        snapshot = await self.newest_run()
        self.assertEqual(result.audit_id, snapshot.admission.run_id)
        assert snapshot.record.finalization is not None
        self.assertIs(snapshot.record.finalization.outcome, TerminalOutcome.COMPLETED)
        self.assertTrue(snapshot.record.finalization.command_started)

    async def test_nonzero_exit_is_recorded_as_failed(self):
        backend = FakeBackend(descriptor(), handle_options={"exit_code": 3})
        runner, backend = self.build_runner(backend)

        result = await self.start_run(runner)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, 3)
        snapshot = await self.newest_run()
        assert snapshot.record.finalization is not None
        self.assertIs(snapshot.record.finalization.outcome, TerminalOutcome.FAILED)
        self.assertEqual(snapshot.record.finalization.exit_code, 3)

    async def test_timeout_terminates_and_records_a_timed_out_row(self):
        backend = FakeBackend(descriptor(), handle_options={"exit_delay": 5.0})
        runner, backend = self.build_runner(backend)

        result = await self.start_run(
            runner,
            prepared_execution=prepared(request(timeout_seconds=0.01)),
        )

        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.termination_reason, "timeout")
        self.assertIsNone(result.exit_code)
        assert backend.handle is not None
        self.assertEqual(backend.handle.terminations, ["timeout"])
        self.assertIn("timeout_reached", self.store.event_calls())
        self.assertIn("termination_started", self.store.event_calls())
        snapshot = await self.newest_run()
        assert snapshot.record.finalization is not None
        self.assertIs(snapshot.record.finalization.outcome, TerminalOutcome.TIMED_OUT)
        self.assertIsNone(snapshot.record.finalization.exit_code)

    async def test_incomplete_cleanup_raises_after_recording_the_outcome(self):
        backend = FakeBackend(
            descriptor(),
            handle_options={"cleanup_complete": False},
        )
        runner, backend = self.build_runner(backend)

        with self.assertRaises(BackendCleanupError):
            await self.start_run(runner)

        snapshot = await self.newest_run()
        assert snapshot.record.finalization is not None
        self.assertIs(snapshot.record.finalization.outcome, TerminalOutcome.CLEANUP_FAILED)
        self.assertIs(
            snapshot.record.finalization.underlying_outcome,
            TerminalOutcome.COMPLETED,
        )


class PureBuilderTests(unittest.TestCase):
    def test_finalization_of_an_unstarted_run_carries_no_output(self):
        output = empty_output()
        material = TerminalMaterial(
            claim=_claim("denied", None, "policy_denied"),
            backend_exit=None,
            output=output,
            audit_output=build_output_evidence(output),
            cleanup=None,
            started_at_monotonic=None,
            finished_at_monotonic=5.0,
        )

        finalization = build_finalization(
            "run_01",
            material,
            finalized_at=NOW,
        )

        self.assertIs(finalization.outcome, TerminalOutcome.POLICY_DENIED)
        self.assertFalse(finalization.command_started)
        self.assertIsNone(finalization.output)
        self.assertIsNone(finalization.exit_code)

    def test_terminated_outcomes_never_carry_an_exit_code(self):
        output = empty_output()
        material = TerminalMaterial(
            claim=_claim("timed_out", "timeout", "timeout"),
            backend_exit=BackendExit(exit_code=None, native_reason="timeout"),
            output=output,
            audit_output=build_output_evidence(output),
            cleanup=CleanupResult(complete=True),
            started_at_monotonic=1.0,
            finished_at_monotonic=3.5,
        )

        finalization = build_finalization("run_02", material, finalized_at=NOW)

        self.assertIs(finalization.outcome, TerminalOutcome.TIMED_OUT)
        self.assertIsNone(finalization.exit_code)
        self.assertEqual(material.duration_seconds, 2.5)

    def test_result_requires_a_finalized_record(self):
        output = empty_output()
        material = TerminalMaterial(
            claim=_claim("completed", None, "backend_exit"),
            backend_exit=BackendExit(exit_code=0),
            output=output,
            audit_output=build_output_evidence(output),
            cleanup=None,
            started_at_monotonic=1.0,
            finished_at_monotonic=2.0,
        )
        record = _pending_record()

        with self.assertRaises(ValueError):
            build_execution_result(record, material, backend="posix")

    def test_material_rejects_an_exit_without_a_start(self):
        output = empty_output()

        with self.assertRaises(ValueError):
            TerminalMaterial(
                claim=_claim("completed", None, "backend_exit"),
                backend_exit=BackendExit(exit_code=0),
                output=output,
                audit_output=build_output_evidence(output),
                cleanup=None,
                started_at_monotonic=None,
                finished_at_monotonic=1.0,
            )


def _claim(status, reason, source):
    return TerminalClaim(
        status=status,
        reason=reason,
        observed_at_monotonic=1.0,
        source=source,
    )


def _pending_record():
    return AuditRunRecord(
        run_id="run_03",
        created_at=NOW,
        updated_at=NOW,
        phase=AuditRunPhase.PENDING,
    )


if __name__ == "__main__":
    unittest.main()
