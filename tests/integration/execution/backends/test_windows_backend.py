from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from truecoder.execution.audit.models import (
    AuditRunPhase,
    BackendResourceIdentifier,
    TerminalOutcome,
)
from truecoder.execution.audit.service import AuditService
from truecoder.execution.audit.store import SQLiteAuditStore
from truecoder.execution.backends.base import BackendStartContext
from truecoder.execution.backends.models import BackendDescriptor
from truecoder.execution.backends.registry import BackendRegistry
from truecoder.execution.backends.windows import WindowsBackend
from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.environment import construct_environment
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

WINDOWS = sys.platform == "win32"
ROOT = Path.cwd().resolve()
HELPER = ROOT / "tests" / "helpers" / "execution" / "windows_job_probe.py"


def descriptor() -> BackendDescriptor:
    return BackendDescriptor(
        name="windows",
        available=True,
        capabilities=BackendCapabilities(
            filesystem_isolation="unsupported",
            network_isolation="unsupported",
            memory_limits="enforced",
            cpu_limits="best_effort",
            process_limits="enforced",
            timeout_enforcement="enforced",
            cancellation="enforced",
            supported_execution_modes=("exec", "shell"),
            supported_filesystem_modes=("host",),
            supported_shells=("powershell",),
        ),
        version="integration",
    )


def request(
    argv: tuple[str, ...],
    *,
    directory: Path,
    timeout_seconds: float = 10.0,
    max_processes: int | None = 16,
) -> ExecutionRequest:
    return ExecutionRequest(
        mode="exec",
        argv=argv,
        script=None,
        working_directory=directory,
        limits=ExecutionLimits(
            timeout_seconds=timeout_seconds,
            max_output_bytes=1024 * 1024,
            max_return_bytes=4096,
            max_processes=max_processes,
            termination_grace_seconds=0,
        ),
        network_access=True,
        filesystem_mode="host",
    )


def prepared(execution_request: ExecutionRequest) -> PreparedExecution:
    return PreparedExecution(
        request=execution_request,
        backend=descriptor(),
        environment=construct_environment(
            platform="windows",
            inherited=os.environ,
            requested=(),
        ),
        resolved_shell=None,
    )


def context(execution_id: str, workspace: Path) -> ExecutionContext:
    return ExecutionContext(
        execution_id=execution_id,
        tool_call_id=f"call-{execution_id}",
        session_id="session-windows-integration",
        turn_id="turn-windows-integration",
        workspace_id="workspace-windows-integration",
        project_root=workspace,
        launched_at_utc=datetime.now(UTC),
    )


def decision(limits: ExecutionLimits) -> PolicyDecision:
    return PolicyDecision(
        allowed=True,
        risk=RiskLevel.LOW,
        requires_approval=False,
        effective_limits=limits,
        requirements=CapabilityRequirements(),
    )


async def register(
    resources: list[BackendResourceIdentifier],
    resource: BackendResourceIdentifier,
) -> None:
    resources.append(resource)


@unittest.skipUnless(WINDOWS, "requires Windows Job Objects")
class WindowsBackendIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="tc-windows-integration-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.backend = WindowsBackend(
            descriptor(),
            host_id="windows-integration-host",
        )

    async def test_terminating_the_job_kills_descendants(self):
        marker = self.directory / "descendant-survived"
        launch = request(
            (sys.executable, str(HELPER), "tree", str(marker)),
            directory=self.directory,
        )
        resources: list[BackendResourceIdentifier] = []
        handle = await self.backend.start(
            prepared(launch),
            launch,
            BackendStartContext(
                execution=context("exec-windows-tree", self.directory),
                audit_run_id="run-windows-tree",
            ),
            CancellationSource().token,
            lambda resource: register(resources, resource),
        )
        output_task = asyncio.create_task(_read_first_stdout_line(handle))
        tree = json.loads((await asyncio.wait_for(output_task, timeout=5)).decode())

        await handle.terminate("cancellation", 0)
        exited = await asyncio.wait_for(handle.wait(), timeout=5)
        self.assertEqual(exited.native_reason, "cancellation")
        self.assertEqual(tuple(tree), ("parent", "child", "grandchild"))
        self.assertEqual(resources, [handle.resource])
        self.assertTrue((await handle.cleanup()).complete)

        await asyncio.sleep(2)
        self.assertFalse(marker.exists())

    async def test_job_process_limit_blocks_descendant_creation(self):
        launch = request(
            (sys.executable, str(HELPER), "process-limit"),
            directory=self.directory,
            max_processes=1,
        )
        handle = await self.backend.start(
            prepared(launch),
            launch,
            BackendStartContext(
                execution=context("exec-windows-process-limit", self.directory),
                audit_run_id="run-windows-process-limit",
            ),
            CancellationSource().token,
            lambda resource: register([], resource),
        )
        try:
            output = b"".join([
                chunk.data
                async for chunk in handle.output()
                if chunk.stream == "stdout"
            ])
            exited = await handle.wait()
            self.assertEqual(exited.exit_code, 0)
            self.assertTrue(output.decode().startswith("blocked:"))
        finally:
            self.assertTrue((await handle.cleanup()).complete)

    async def test_timeout_is_durable_through_the_full_service(self):
        database = self.directory / "audit.sqlite3"
        audit = AuditService(SQLiteAuditStore(database))
        registry = ExecutionRegistry()
        service = ExecutionService(
            registry,
            runner=ExecutionRunner(
                audit,
                BackendRegistry((self.backend,)),
                registry=registry,
                safety_deadline_seconds=5,
            ),
            audit=audit,
        )
        launch = request(
            (
                sys.executable,
                "-c",
                "import time; print('ready', flush=True); time.sleep(60)",
            ),
            directory=self.directory,
            timeout_seconds=0.3,
        )

        result = await service.run_prepared(
            prepared(launch),
            decision(launch.limits),
            context("exec-windows-timeout", self.directory),
        )

        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.termination_reason, "timeout")
        self.assertEqual(result.stdout.strip(), "ready")
        self.assertEqual(await registry.active_execution_ids(), ())
        snapshot = await audit.get_run(result.audit_id)
        self.assertIs(snapshot.record.phase, AuditRunPhase.TERMINAL)
        assert snapshot.record.finalization is not None
        self.assertIs(snapshot.record.finalization.outcome, TerminalOutcome.TIMED_OUT)


async def _read_first_stdout_line(handle) -> bytes:
    buffer = bytearray()
    async for chunk in handle.output():
        if chunk.stream != "stdout":
            continue
        buffer.extend(chunk.data)
        if b"\n" in buffer:
            return bytes(buffer.split(b"\n", 1)[0])
    raise AssertionError("process exited without producing a line")


if __name__ == "__main__":
    unittest.main()
