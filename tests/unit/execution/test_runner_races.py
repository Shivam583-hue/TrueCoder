from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from pathlib import Path

from tests.fakes.execution import (
    AuditSpy,
    CollectingEventSink,
    FakeApproval,
    FakeClock,
    PreviewCollector,
    ScriptedBackend,
)
from truecoder.execution.audit.models import AuditRunPhase, TerminalOutcome
from truecoder.execution.audit.service import AuditService
from truecoder.execution.backends.models import (
    BackendDescriptor,
    BackendExit,
    BackendOutputChunk,
)
from truecoder.execution.backends.registry import BackendRegistry
from truecoder.execution.environment import construct_environment
from truecoder.execution.errors import (
    AuditPersistenceError,
    AuditUnavailableError,
    BackendCleanupError,
    BackendOperationError,
    BackendStartError,
)
from truecoder.execution.models import (
    BackendCapabilities,
    CapabilityRequirements,
    ExecutionContext,
    ExecutionLimits,
    ExecutionRequest,
    PolicyDecision,
    PolicyReason,
    RiskLevel,
)
from truecoder.execution.preparation import PreparedExecution
from truecoder.execution.registry import CancellationOutcome, ExecutionRegistry
from truecoder.execution.runner import ExecutionRunner, choose_terminal_candidate
from truecoder.execution.service import ExecutionService

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
ROOT = Path.cwd().resolve()
TIMEOUT_SECONDS = 30.0


def descriptor(version: str = "test") -> BackendDescriptor:
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
        version=version,
    )


def request(
    *,
    max_output_bytes: int = 4096,
    max_return_bytes: int = 4096,
) -> ExecutionRequest:
    return ExecutionRequest(
        mode="exec",
        argv=("python", "-V"),
        script=None,
        working_directory=ROOT,
        limits=ExecutionLimits(
            timeout_seconds=TIMEOUT_SECONDS,
            max_output_bytes=max_output_bytes,
            max_return_bytes=max_return_bytes,
            termination_grace_seconds=0.01,
        ),
        network_access=True,
        filesystem_mode="host",
    )


def context(execution_id: str = "exec-race-01") -> ExecutionContext:
    return ExecutionContext(
        execution_id=execution_id,
        tool_call_id=f"call-{execution_id}",
        session_id="session-01",
        turn_id="turn-01",
        workspace_id="workspace-01",
        project_root=ROOT,
        launched_at_utc=NOW,
    )


def prepared(
    execution_request: ExecutionRequest | None = None,
    *,
    backend: BackendDescriptor | None = None,
) -> PreparedExecution:
    effective = execution_request or request()
    return PreparedExecution(
        request=effective,
        backend=backend or descriptor(),
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
                code="blocked",
                message="not permitted",
                rule_id="rule-01",
            ),
        ),
    )


class OrchestrationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.spy = AuditSpy()
        self.audit = AuditService(
            self.spy,
            run_id_factory=lambda: self.spy.run_id,
            clock=lambda: datetime.now(UTC),
        )
        self.registry = ExecutionRegistry()
        self.sink = CollectingEventSink()
        self.preview = PreviewCollector()

    def build(
        self,
        backend: ScriptedBackend | None = None,
        *,
        approval_gate=None,
        safety_deadline_seconds: float = 0.05,
    ) -> tuple[ExecutionRunner, ScriptedBackend]:
        chosen = backend or ScriptedBackend(descriptor())
        runner = ExecutionRunner(
            self.audit,
            BackendRegistry((chosen,)),
            registry=self.registry,
            approval_gate=approval_gate,
            clock=self.clock,
            event_sink=self.sink,
            preview_sink=self.preview,
            safety_deadline_seconds=safety_deadline_seconds,
        )
        return runner, chosen

    async def wait_until_watching(self, backend: ScriptedBackend) -> None:
        await backend.started.wait()
        while self.clock.pending_sleepers == 0:
            await asyncio.sleep(0)

    async def run_once(self, runner: ExecutionRunner, **kwargs):
        return await runner.run(
            kwargs.pop("prepared_execution", prepared()),
            kwargs.pop("policy_decision", decision()),
            kwargs.pop("execution_context", context()),
        )

    async def assert_universal_postconditions(
        self,
        backend: ScriptedBackend,
        *,
        handle_returned: bool,
    ) -> None:
        self.assertLessEqual(backend.start_count, 1)
        self.assertLessEqual(self.spy.finalize_count, 1)
        self.assertEqual(await self.registry.active_execution_ids(), ())

        handle = backend.handle
        if handle is None:
            self.assertFalse(handle_returned)
            return

        self.assertLessEqual(handle.output_claim_count, 1)
        self.assertLessEqual(handle.terminate_count, 1)
        self.assertEqual(handle.cleanup_count, 1 if handle_returned else 0)

        terminal = [
            stage
            for stage in self.sink.stages()
            if stage
            in {
                "completed",
                "failed",
                "timed_out",
                "cancelled",
                "denied",
                "limit_exceeded",
                "failed_to_start",
            }
        ]
        self.assertLessEqual(len(terminal), 1)


class SameTickRaceTests(unittest.TestCase):
    def candidate(self, **kwargs):
        base = {
            "done": set(),
            "backend_exit": None,
            "cancellation": None,
            "output_limit": False,
            "timeout": False,
            "observed_at": 5.0,
        }
        base.update(kwargs)
        return choose_terminal_candidate(**base)

    def test_natural_exit_beats_timeout(self):
        claim = self.candidate(
            backend_exit=BackendExit(exit_code=0),
            timeout=True,
        )

        self.assertEqual(claim.source, "backend_exit")
        self.assertEqual(claim.status, "completed")

    def test_output_limit_beats_timeout(self):
        claim = self.candidate(output_limit=True, timeout=True)

        self.assertEqual(claim.source, "output_limit")
        self.assertEqual(claim.reason, "output_limit")

    def test_cancellation_beats_timeout(self):
        claim = self.candidate(cancellation="user", timeout=True)

        self.assertEqual(claim.source, "cancellation")
        self.assertEqual(claim.reason, "cancellation")

    def test_natural_exit_beats_cancellation(self):
        claim = self.candidate(
            backend_exit=BackendExit(exit_code=3),
            cancellation="user",
        )

        self.assertEqual(claim.source, "backend_exit")
        self.assertEqual(claim.status, "failed")

    def test_resource_limit_beats_cancellation_and_timeout(self):
        claim = self.candidate(
            backend_exit=BackendExit(exit_code=None, native_reason="memory_limit"),
            cancellation="user",
            timeout=True,
        )

        self.assertEqual(claim.source, "resource_limit")
        self.assertEqual(claim.reason, "memory_limit")

    def test_every_signal_at_once_resolves_to_the_exit(self):
        claim = self.candidate(
            backend_exit=BackendExit(exit_code=0),
            cancellation="shutdown",
            output_limit=True,
            timeout=True,
            pump_failed=True,
        )

        self.assertEqual(claim.source, "backend_exit")

    def test_no_signal_is_an_invalid_state(self):
        from truecoder.execution.errors import InvalidExecutionStateError

        with self.assertRaises(InvalidExecutionStateError):
            self.candidate()


class TerminalRouteTests(OrchestrationTestCase):
    async def test_exit_zero_completes_with_exact_output_and_audit_id(self):
        backend = ScriptedBackend(
            descriptor(),
            handle_options={
                "chunks": (
                    BackendOutputChunk(stream="stdout", data=b"hello "),
                    BackendOutputChunk(stream="stdout", data=b"world"),
                    BackendOutputChunk(stream="stderr", data=b"warn"),
                ),
                "exit_code": 0,
            },
        )
        runner, backend = self.build(backend)
        assert backend.handle is None

        result = await self.run_once(runner)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "hello world")
        self.assertEqual(result.stderr, "warn")
        self.assertEqual(result.audit_id, self.spy.run_id)
        self.assertEqual(result.stdout_bytes, 11)
        self.assertEqual(result.stderr_bytes, 4)
        assert backend.handle is not None
        self.assertEqual(backend.handle.terminate_count, 0)
        await self.assert_universal_postconditions(backend, handle_returned=True)

    async def test_nonzero_exit_is_a_result_not_an_exception(self):
        backend = ScriptedBackend(descriptor(), handle_options={"exit_code": 9})
        runner, backend = self.build(backend)

        result = await self.run_once(runner)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, 9)
        assert self.spy.finalization is not None
        self.assertIs(self.spy.finalization.outcome, TerminalOutcome.FAILED)
        await self.assert_universal_postconditions(backend, handle_returned=True)

    async def test_empty_output_still_closes_both_streams(self):
        runner, _backend = self.build()

        result = await self.run_once(runner)

        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        assert self.spy.finalization is not None
        assert self.spy.finalization.output is not None
        self.assertTrue(self.spy.finalization.output.complete)

    async def test_unicode_split_across_chunks_renders_identically(self):
        text = "héllo wörld ✅"
        encoded = text.encode("utf-8")
        chunks = tuple(
            BackendOutputChunk(stream="stdout", data=encoded[index : index + 1])
            for index in range(len(encoded))
        )
        backend = ScriptedBackend(descriptor(), handle_options={"chunks": chunks})
        runner, backend = self.build(backend)

        result = await self.run_once(runner)

        self.assertEqual(result.stdout, text)
        self.assertEqual(result.stdout_bytes, len(encoded))

    async def test_secret_values_are_redacted_from_returned_text(self):
        effective = request()
        environment = construct_environment(
            platform="posix",
            inherited={"GITHUB_TOKEN": "super-secret-value"},
            requested=(),
        )
        launch = PreparedExecution(
            request=effective,
            backend=descriptor(),
            environment=environment,
            resolved_shell=None,
        )
        backend = ScriptedBackend(
            descriptor(),
            handle_options={
                "chunks": (
                    BackendOutputChunk(
                        stream="stdout",
                        data=b"token=super-secret-value done",
                    ),
                ),
            },
        )
        runner, backend = self.build(backend)

        result = await self.run_once(runner, prepared_execution=launch)

        self.assertNotIn("super-secret-value", result.stdout)
        self.assertIn("[REDACTED]", result.stdout)
        assert self.spy.finalization is not None
        assert self.spy.finalization.output is not None
        self.assertEqual(self.spy.finalization.output.stdout_bytes, 29)

    async def test_preview_sink_receives_text_without_service_buffering(self):
        backend = ScriptedBackend(
            descriptor(),
            handle_options={
                "chunks": (BackendOutputChunk(stream="stdout", data=b"streamed"),),
            },
        )
        runner, backend = self.build(backend)

        await self.run_once(runner)

        self.assertEqual("".join(self.preview.texts), "streamed")


class InterventionRouteTests(OrchestrationTestCase):
    async def test_timeout_claims_timed_out_and_terminates_once(self):
        backend = ScriptedBackend(
            descriptor(),
            handle_options={"gate_exit": True},
        )
        runner, backend = self.build(backend)
        run = asyncio.create_task(self.run_once(runner))

        await self.wait_until_watching(backend)
        assert backend.handle is not None
        await self.clock.advance(TIMEOUT_SECONDS)
        result = await run

        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.termination_reason, "timeout")
        self.assertEqual(backend.handle.terminate_count, 1)
        self.assertEqual(backend.handle.termination_reasons, ["timeout"])
        self.assertIn("timeout_reached", self.spy.event_names())
        await self.assert_universal_postconditions(backend, handle_returned=True)

    async def test_output_limit_claims_limit_exceeded_and_keeps_draining(self):
        backend = ScriptedBackend(
            descriptor(),
            handle_options={
                "chunks": (
                    BackendOutputChunk(stream="stdout", data=b"0123456789"),
                    BackendOutputChunk(stream="stdout", data=b"abcdefghij"),
                ),
                "gate_exit": True,
            },
        )
        runner, backend = self.build(backend)

        result = await self.run_once(
            runner,
            prepared_execution=prepared(
                request(max_output_bytes=8, max_return_bytes=8),
            ),
        )

        self.assertEqual(result.status, "limit_exceeded")
        self.assertEqual(result.termination_reason, "output_limit")
        assert backend.handle is not None
        self.assertEqual(backend.handle.terminate_count, 1)
        self.assertEqual(result.stdout_bytes, 20)
        await self.assert_universal_postconditions(backend, handle_returned=True)

    async def test_backend_resource_limit_maps_to_the_exact_limit(self):
        for reason in ("memory_limit", "cpu_limit", "process_limit"):
            with self.subTest(reason=reason):
                self.setUp()
                backend = ScriptedBackend(
                    descriptor(),
                    handle_options={"exit_code": None, "native_reason": reason},
                )
                runner, backend = self.build(backend)

                result = await self.run_once(runner)

                self.assertEqual(result.status, "limit_exceeded")
                self.assertEqual(result.termination_reason, reason)
                self.assertEqual(backend.handle.terminate_count, 1)

    async def test_natural_exit_before_timeout_never_terminates(self):
        backend = ScriptedBackend(descriptor(), handle_options={"exit_code": 0})
        runner, backend = self.build(backend)

        result = await self.run_once(runner)

        self.assertEqual(result.status, "completed")
        assert backend.handle is not None
        self.assertEqual(backend.handle.terminate_count, 0)
        self.assertNotIn("timeout_reached", self.spy.event_names())


class BrokenBackendTests(OrchestrationTestCase):
    async def test_output_iterator_failure_marks_evidence_incomplete(self):
        backend = ScriptedBackend(
            descriptor(),
            handle_options={
                "chunks": (BackendOutputChunk(stream="stdout", data=b"partial"),),
                "output_error": OSError("pipe exploded"),
                "exit_code": 0,
            },
        )
        runner, backend = self.build(backend)

        result = await self.run_once(runner)

        assert self.spy.finalization is not None
        assert self.spy.finalization.output is not None
        self.assertFalse(self.spy.finalization.output.complete)
        self.assertEqual(result.stdout, "partial")
        await self.assert_universal_postconditions(backend, handle_returned=True)

    async def test_wait_failure_becomes_an_infrastructure_shutdown(self):
        backend = ScriptedBackend(
            descriptor(),
            handle_options={"wait_error": OSError("protocol error")},
        )
        runner, backend = self.build(backend)

        result = await self.run_once(runner)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.termination_reason, "shutdown")
        self.assertIsNone(result.exit_code)
        await self.assert_universal_postconditions(backend, handle_returned=True)

    async def test_terminate_failure_is_an_infrastructure_error_not_a_timeout(self):
        backend = ScriptedBackend(
            descriptor(),
            handle_options={
                "terminate_error": OSError("kill failed"),
                "gate_exit": True,
            },
        )
        runner, backend = self.build(backend)
        run = asyncio.create_task(self.run_once(runner))
        await self.wait_until_watching(backend)
        await self.clock.advance(TIMEOUT_SECONDS)

        with self.assertRaises(BackendOperationError):
            await run

        assert self.spy.finalization is not None
        self.assertIs(self.spy.finalization.outcome, TerminalOutcome.TIMED_OUT)
        await self.assert_universal_postconditions(backend, handle_returned=True)

    async def test_a_raising_cleanup_is_recorded_as_incomplete(self):
        backend = ScriptedBackend(
            descriptor(),
            handle_options={
                "cleanup_error": OSError("cleanup exploded"),
                "exit_code": 0,
            },
        )
        runner, backend = self.build(backend)

        with self.assertRaises(BackendCleanupError):
            await self.run_once(runner)

        assert self.spy.finalization is not None
        self.assertIs(self.spy.finalization.outcome, TerminalOutcome.CLEANUP_FAILED)
        self.assertEqual(await self.registry.active_execution_ids(), ())

    async def test_incomplete_cleanup_raises_after_recording_the_outcome(self):
        backend = ScriptedBackend(
            descriptor(),
            handle_options={"cleanup_complete": False, "exit_code": 0},
        )
        runner, backend = self.build(backend)

        with self.assertRaises(BackendCleanupError):
            await self.run_once(runner)

        assert self.spy.finalization is not None
        self.assertIs(self.spy.finalization.outcome, TerminalOutcome.CLEANUP_FAILED)
        self.assertIs(
            self.spy.finalization.underlying_outcome,
            TerminalOutcome.COMPLETED,
        )
        self.assertEqual(await self.registry.active_execution_ids(), ())

    async def test_pipes_that_never_reach_eof_mark_output_incomplete(self):
        stalled = ScriptedBackend(
            descriptor(),
            handle_options={"exit_code": 0, "gate_output": True},
        )
        runner, backend = self.build(stalled, safety_deadline_seconds=0.02)
        run = asyncio.create_task(self.run_once(runner))
        await backend.started.wait()
        assert backend.handle is not None
        result = await run

        self.assertEqual(result.status, "completed")
        assert self.spy.finalization is not None
        assert self.spy.finalization.output is not None
        self.assertFalse(self.spy.finalization.output.complete)

    async def test_descriptor_change_between_selection_and_start_fails_closed(self):
        backend = ScriptedBackend(descriptor(version="v1"))
        runner, backend = self.build(backend)
        backend.set_descriptor(descriptor(version="v2"))

        with self.assertRaises(Exception) as caught:
            await self.run_once(runner)

        self.assertEqual(backend.start_count, 0)
        self.assertIn("no longer matches", str(caught.exception))
        self.assertEqual(await self.registry.active_execution_ids(), ())


class StartupRouteTests(OrchestrationTestCase):
    async def test_registrar_attaches_before_the_gate_opens(self):
        runner, backend = self.build()

        await self.run_once(runner)

        self.assertIsNotNone(self.spy.resource)
        self.assertTrue(backend.target_gate_opened)
        order = self.spy.calls.index("attach_resource")
        self.assertLess(order, self.spy.calls.index("mark_running"))

    async def test_backend_failure_before_registration_starts_nothing(self):
        backend = ScriptedBackend(
            descriptor(),
            fail_before_registration=BackendStartError(
                "no executable",
                backend="posix",
                operation="start",
            ),
        )
        runner, backend = self.build(backend)

        result = await self.run_once(runner)

        self.assertEqual(result.status, "failed_to_start")
        self.assertIsNone(self.spy.resource)
        self.assertEqual(await self.registry.active_execution_ids(), ())

    async def test_backend_failure_after_registration_still_finalizes(self):
        backend = ScriptedBackend(
            descriptor(),
            fail_after_registration=BackendStartError(
                "exec failed",
                backend="posix",
                operation="start",
            ),
        )
        runner, backend = self.build(backend)

        result = await self.run_once(runner)

        self.assertEqual(result.status, "failed_to_start")
        self.assertIsNotNone(self.spy.resource)
        assert self.spy.finalization is not None
        self.assertIsNotNone(self.spy.finalization.resource)


class AuditFailureMatrixTests(OrchestrationTestCase):
    async def test_every_audit_operation_fails_closed(self):
        operations = (
            ("admit", AuditUnavailableError, False),
            ("append_event:policy_allowed", AuditPersistenceError, False),
            ("append_event:backend_starting", None, False),
            ("attach_resource", BackendStartError, True),
            ("mark_running", AuditPersistenceError, True),
            ("finalize", AuditPersistenceError, True),
        )

        for operation, expected, backend_had_authority in operations:
            with self.subTest(operation=operation):
                self.setUp()
                runner, backend = self.build()
                self.spy.fail_on.add(operation)

                if expected is None:
                    result = await self.run_once(runner)
                    self.assertEqual(result.status, "failed_to_start")
                else:
                    with self.assertRaises(expected):
                        await self.run_once(runner)

                self.assertEqual(backend.start_count, 1 if backend_had_authority else 0)
                self.assertFalse(
                    backend.target_gate_opened and operation == "attach_resource"
                )
                self.assertEqual(await self.registry.active_execution_ids(), ())

    async def test_runtime_event_failure_is_fatal(self):
        backend = ScriptedBackend(descriptor(), handle_options={"gate_exit": True})
        runner, backend = self.build(backend)
        self.spy.fail_on.add("append_event:timeout_reached")
        run = asyncio.create_task(self.run_once(runner))
        await self.wait_until_watching(backend)
        await self.clock.advance(TIMEOUT_SECONDS)

        with self.assertRaises(AuditPersistenceError):
            await run

        self.assertEqual(self.spy.phase, AuditRunPhase.TERMINAL)
        await self.assert_universal_postconditions(backend, handle_returned=True)


class ApprovalRouteTests(OrchestrationTestCase):
    async def test_rejection_finalizes_denied_without_a_backend(self):
        approval = FakeApproval(approve=False)
        runner, backend = self.build(approval_gate=approval)

        result = await self.run_once(runner)

        self.assertEqual(result.status, "denied")
        self.assertEqual(backend.start_count, 0)
        self.assertEqual(approval.responses, 1)
        self.assertIn("approval_rejected", self.spy.event_names())

    async def test_approval_sees_the_exact_prepared_contract(self):
        approval = FakeApproval(approve=True)
        runner, backend = self.build(approval_gate=approval)
        launch = prepared()

        await self.run_once(runner, prepared_execution=launch)

        self.assertIs(approval.requests[0], launch)
        self.assertIs(backend.prepared_seen[0], launch)

    async def test_cancellation_while_approval_is_open_never_starts(self):
        approval = FakeApproval(approve=True, gate=True)
        runner, backend = self.build(approval_gate=approval)
        service = ExecutionService(
            self.registry,
            runner=runner,
            audit=self.audit,
        )
        run = asyncio.create_task(self.run_once(runner))
        await approval.requested.wait()

        outcome = await service.cancel("exec-race-01")
        approval.release()
        result = await run

        self.assertIs(outcome, CancellationOutcome.NOT_FOUND)
        self.assertEqual(result.status, "completed")
        self.assertEqual(backend.start_count, 1)


class TaskOwnershipTests(OrchestrationTestCase):
    async def test_no_task_escapes_a_successful_run(self):
        before = _live_tasks()
        backend = ScriptedBackend(
            descriptor(),
            handle_options={
                "chunks": (BackendOutputChunk(stream="stdout", data=b"x"),),
                "exit_code": 0,
            },
        )
        runner, backend = self.build(backend)

        await self.run_once(runner)
        await _settle()

        self.assertEqual(_live_tasks() - before, set())

    async def test_no_task_escapes_a_terminated_run(self):
        before = _live_tasks()
        runner, backend = self.build(
            ScriptedBackend(descriptor(), handle_options={"gate_exit": True}),
        )
        run = asyncio.create_task(self.run_once(runner))
        await self.wait_until_watching(backend)
        await self.clock.advance(TIMEOUT_SECONDS)
        await run
        await _settle()

        self.assertEqual(_live_tasks() - before - {run}, set())

    async def test_external_cancellation_of_the_run_leaves_no_tasks(self):
        backend = ScriptedBackend(
            descriptor(),
            handle_options={"gate_exit": True},
            gate_start=True,
        )
        runner, backend = self.build(backend)
        before = _live_tasks()
        run = asyncio.create_task(self.run_once(runner))
        await backend.registrar_completed.wait()

        run.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await run
        await _settle()

        self.assertEqual(_live_tasks() - before - {run}, set())
        self.assertEqual(await self.registry.active_execution_ids(), ())


async def _settle(turns: int = 6) -> None:
    for _turn in range(turns):
        await asyncio.sleep(0)


def _live_tasks() -> set[asyncio.Task]:
    return {task for task in asyncio.all_tasks() if not task.done()}


if __name__ == "__main__":
    unittest.main()
