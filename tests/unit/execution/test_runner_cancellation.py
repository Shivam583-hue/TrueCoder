from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime

from tests.fakes.execution import AuditSpy, FakeApproval, ScriptedBackend
from tests.unit.execution.test_runner_races import (
    OrchestrationTestCase,
    context,
    decision,
    descriptor,
    prepared,
)
from truecoder.execution.audit.models import TerminalOutcome
from truecoder.execution.audit.service import AuditService
from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.errors import AuditPersistenceError
from truecoder.execution.registry import CancellationOutcome
from truecoder.execution.service import ExecutionService

EXECUTION_ID = "exec-race-01"


class CancellationRoutingTests(OrchestrationTestCase):
    def service(self) -> ExecutionService:
        return ExecutionService(self.registry, audit=self.audit)

    async def test_cancel_during_output_terminates_once(self):
        backend = ScriptedBackend(descriptor(), handle_options={"gate_exit": True})
        runner, backend = self.build(backend)
        service = self.service()
        run = asyncio.create_task(self.run_once(runner))
        await self.wait_until_watching(backend)

        outcome = await service.cancel(EXECUTION_ID)
        result = await run

        self.assertIs(outcome, CancellationOutcome.REQUESTED)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.termination_reason, "cancellation")
        assert backend.handle is not None
        self.assertEqual(backend.handle.terminate_count, 1)
        await self.assert_universal_postconditions(backend, handle_returned=True)

    async def test_caller_owned_source_is_the_registered_control(self):
        backend = ScriptedBackend(descriptor(), handle_options={"gate_exit": True})
        runner, backend = self.build(backend)
        source = CancellationSource()
        run = asyncio.create_task(
            runner.run_prepared(
                prepared(),
                decision(),
                context(),
                cancellation_source=source,
            )
        )
        await self.wait_until_watching(backend)

        entry = await self.registry.get(EXECUTION_ID)
        assert entry is not None
        self.assertIs(entry.cancellation_source, source)
        source.cancel("agent_cancelled")
        result = await run

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.termination_reason, "cancellation")
        assert backend.handle is not None
        self.assertEqual(backend.handle.terminate_count, 1)

    async def test_shutdown_reason_maps_to_a_shutdown_cancellation(self):
        backend = ScriptedBackend(descriptor(), handle_options={"gate_exit": True})
        runner, backend = self.build(backend)
        service = self.service()
        run = asyncio.create_task(self.run_once(runner))
        await self.wait_until_watching(backend)

        await service.cancel(EXECUTION_ID, reason="shutdown")
        result = await run

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.termination_reason, "shutdown")
        assert self.spy.finalization is not None
        self.assertIs(self.spy.finalization.outcome, TerminalOutcome.CANCELLED)

    async def test_two_concurrent_cancels_produce_one_request(self):
        backend = ScriptedBackend(descriptor(), handle_options={"gate_exit": True})
        runner, backend = self.build(backend)
        service = self.service()
        run = asyncio.create_task(self.run_once(runner))
        await self.wait_until_watching(backend)

        first, second = await asyncio.gather(
            service.cancel(EXECUTION_ID),
            service.cancel(EXECUTION_ID),
        )
        result = await run

        outcomes = {first, second}
        self.assertIn(CancellationOutcome.REQUESTED, outcomes)
        self.assertIn(CancellationOutcome.ALREADY_REQUESTED, outcomes)
        self.assertEqual(result.status, "cancelled")
        assert backend.handle is not None
        self.assertEqual(backend.handle.terminate_count, 1)

    async def test_second_cancellation_reports_already_requested(self):
        backend = ScriptedBackend(descriptor(), handle_options={"gate_exit": True})
        runner, backend = self.build(backend)
        service = self.service()
        run = asyncio.create_task(self.run_once(runner))
        await self.wait_until_watching(backend)

        first = await service.cancel(EXECUTION_ID)
        second = await service.cancel(EXECUTION_ID)
        await run

        self.assertIs(first, CancellationOutcome.REQUESTED)
        self.assertIs(second, CancellationOutcome.ALREADY_REQUESTED)

    async def test_cancelling_an_unknown_execution_is_not_found(self):
        service = self.service()

        outcome = await service.cancel("exec-does-not-exist")

        self.assertIs(outcome, CancellationOutcome.NOT_FOUND)

    async def test_cancel_after_natural_exit_cannot_replace_the_winner(self):
        backend = ScriptedBackend(descriptor(), handle_options={"exit_code": 0})
        runner, backend = self.build(backend)
        service = self.service()

        result = await self.run_once(runner)
        outcome = await service.cancel(EXECUTION_ID)

        self.assertEqual(result.status, "completed")
        self.assertIs(outcome, CancellationOutcome.NOT_FOUND)
        assert self.spy.finalization is not None
        self.assertIs(self.spy.finalization.outcome, TerminalOutcome.COMPLETED)

    async def test_cancel_while_backend_start_is_gated_reaches_the_token(self):
        backend = ScriptedBackend(
            descriptor(),
            handle_options={"exit_code": 0},
            gate_start=True,
        )
        runner, backend = self.build(backend)
        service = self.service()
        run = asyncio.create_task(self.run_once(runner))
        await backend.registrar_completed.wait()

        outcome = await service.cancel(EXECUTION_ID)
        backend.start_allowed_to_return.set()
        result = await run

        self.assertIs(outcome, CancellationOutcome.REQUESTED)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.termination_reason, "cancellation")
        assert self.spy.finalization is not None
        self.assertIs(
            self.spy.finalization.outcome,
            TerminalOutcome.FAILED_TO_START,
        )
        self.assertFalse(backend.target_gate_opened)
        self.assertEqual(await self.registry.active_execution_ids(), ())

    async def test_cancellation_never_depends_on_event_delivery(self):
        backend = ScriptedBackend(descriptor(), handle_options={"gate_exit": True})
        runner, backend = self.build(backend)
        service = self.service()
        run = asyncio.create_task(self.run_once(runner))
        await self.wait_until_watching(backend)
        entry = await self.registry.get(EXECUTION_ID)
        self.spy.fail_on.add("append_event:cancellation_requested")

        outcome = await service.cancel(EXECUTION_ID)

        self.assertIs(outcome, CancellationOutcome.REQUESTED)
        assert entry is not None
        self.assertTrue(entry.cancellation_source.token.cancelled)

        with self.assertRaises(AuditPersistenceError):
            await run

    async def test_the_active_entry_carries_the_audit_handle(self):
        backend = ScriptedBackend(descriptor(), handle_options={"gate_exit": True})
        runner, backend = self.build(backend)
        run = asyncio.create_task(self.run_once(runner))
        await self.wait_until_watching(backend)

        entry = await self.registry.get(EXECUTION_ID)
        assert entry is not None
        assert entry.audit_handle is not None
        self.assertEqual(entry.audit_handle.run_id, self.spy.run_id)

        entry.cancellation_source.cancel("user")
        await run


class ApprovalInterruptionTests(OrchestrationTestCase):
    async def test_a_rejected_approval_never_reaches_project_code(self):
        approval = FakeApproval(approve=True, gate=True)
        backend = ScriptedBackend(descriptor(), handle_options={"exit_code": 0})
        runner, backend = self.build(backend, approval_gate=approval)
        run = asyncio.create_task(self.run_once(runner))
        await approval.requested.wait()

        approval.release(approve=False)
        result = await run

        self.assertEqual(result.status, "denied")
        self.assertEqual(backend.start_count, 0)
        self.assertFalse(backend.target_gate_opened)


class ServiceCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_service_without_a_runner_refuses_to_run(self):
        service = ExecutionService()

        with self.assertRaises(RuntimeError):
            await service.run_prepared(prepared(), decision(), context())

    async def test_cancellation_without_audit_still_signals_the_token(self):
        service = ExecutionService()
        entry = await service.register(context())

        outcome = await service.cancel(entry.context.execution_id)

        self.assertIs(outcome, CancellationOutcome.REQUESTED)
        self.assertTrue(entry.cancellation_source.token.cancelled)

    async def test_an_entry_without_a_handle_records_nothing(self):
        spy = AuditSpy()
        audit = AuditService(
            spy,
            run_id_factory=lambda: spy.run_id,
            clock=lambda: datetime.now(UTC),
        )
        service = ExecutionService(audit=audit)
        entry = await service.register(context())

        outcome = await service.cancel(entry.context.execution_id)

        self.assertIs(outcome, CancellationOutcome.REQUESTED)
        self.assertEqual(spy.calls, [])


if __name__ == "__main__":
    unittest.main()
