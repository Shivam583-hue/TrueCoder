from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.errors import InvalidExecutionStateError
from truecoder.execution.models import ExecutionContext
from truecoder.execution.registry import (
    ActiveExecution,
    CancellationOutcome,
    ExecutionRegistry,
)
from truecoder.execution.service import ExecutionService


def execution_context(execution_id: str = "exec_01") -> ExecutionContext:
    return ExecutionContext(
        execution_id=execution_id,
        tool_call_id="call_01",
        session_id="session_01",
        turn_id="turn_01",
        workspace_id="workspace_01",
        project_root=Path.cwd().resolve(),
        launched_at_utc=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )


def active_execution(execution_id: str = "exec_01") -> ActiveExecution:
    return ActiveExecution(
        context=execution_context(execution_id),
        cancellation_source=CancellationSource(),
    )


class ExecutionRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_registers_looks_up_and_unregisters_exact_entry(self):
        registry = ExecutionRegistry()
        entry = active_execution()

        await registry.register(entry)

        self.assertIs(await registry.get("exec_01"), entry)
        self.assertEqual(await registry.active_execution_ids(), ("exec_01",))
        self.assertTrue(await registry.unregister("exec_01", expected=entry))
        self.assertIsNone(await registry.get("exec_01"))

    async def test_rejects_duplicate_execution_ids(self):
        registry = ExecutionRegistry()
        first = active_execution()
        second = active_execution()
        await registry.register(first)

        with self.assertRaises(InvalidExecutionStateError):
            await registry.register(second)

        self.assertIs(await registry.get("exec_01"), first)

    async def test_cancellation_is_addressed_by_execution_id(self):
        registry = ExecutionRegistry()
        entry = active_execution()
        await registry.register(entry)

        first = await registry.request_cancellation("exec_01", reason="user")
        second = await registry.request_cancellation("exec_01", reason="shutdown")
        missing = await registry.request_cancellation("missing", reason="user")

        self.assertIs(first, CancellationOutcome.REQUESTED)
        self.assertIs(second, CancellationOutcome.ALREADY_REQUESTED)
        self.assertIs(missing, CancellationOutcome.NOT_FOUND)
        self.assertEqual(entry.cancellation_source.token.reason, "user")

    async def test_stale_cleanup_cannot_remove_another_entry(self):
        registry = ExecutionRegistry()
        current = active_execution()
        stale = active_execution()
        await registry.register(current)

        self.assertFalse(await registry.unregister("exec_01", expected=stale))
        self.assertIs(await registry.get("exec_01"), current)

    async def test_service_exposes_registration_lookup_and_cancellation(self):
        service = ExecutionService()
        entry = await service.register(execution_context())

        self.assertIs(await service.lookup("exec_01"), entry)
        self.assertIs(
            await service.cancel("exec_01"),
            CancellationOutcome.REQUESTED,
        )
        self.assertTrue(await service.unregister(entry))
        self.assertIsNone(await service.lookup("exec_01"))


if __name__ == "__main__":
    unittest.main()
