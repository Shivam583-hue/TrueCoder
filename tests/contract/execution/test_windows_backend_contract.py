from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from tests.contract.execution.backend_contract import (
    BackendContractCase,
    BackendContractMixin,
    BackendContractTestCase,
    BackendContractTracker,
    TrackingBackend,
)
from truecoder.execution.backends.base import BackendStartContext
from truecoder.execution.backends.models import (
    BackendDescriptor,
    BackendExit,
    BackendOutputChunk,
    DiscoveredProgram,
)
from truecoder.execution.backends.windows import WindowsBackend
from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.environment import construct_environment
from truecoder.execution.models import (
    BackendCapabilities,
    ExecutionContext,
    ExecutionLimits,
    ExecutionRequest,
)
from truecoder.execution.preparation import PreparedExecution

WINDOWS = sys.platform == "win32"


def capabilities() -> BackendCapabilities:
    return BackendCapabilities(
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
    )


@unittest.skipUnless(WINDOWS, "the windows backend requires a windows host")
class WindowsBackendContractTests(
    BackendContractMixin,
    BackendContractTestCase,
):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="tc-contract-windows-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)

        self.descriptor = BackendDescriptor(
            name="windows",
            available=True,
            capabilities=capabilities(),
            version="contract",
        )
        executable = shutil.which("powershell") or shutil.which("pwsh")
        if executable is None:
            self.skipTest("no PowerShell interpreter is available")
        self.shells = (
            DiscoveredProgram(
                name=Path(executable).name,
                path=Path(executable),
                shell_kind="powershell",
                version="contract",
            ),
        )
        self.backend = WindowsBackend(
            self.descriptor,
            shells=self.shells,
            host_id="contract-host",
        )

    async def make_backend_case(
        self,
        *,
        exit_code: int = 0,
    ) -> BackendContractCase:
        tracker = BackendContractTracker()
        request = self._request(
            f'[Console]::Out.Write("hello`n"); exit {exit_code}',
        )
        return BackendContractCase(
            backend=TrackingBackend(self.backend, tracker),
            prepared=self._prepared(request),
            request=request,
            context=self._context("success"),
            cancellation=CancellationSource().token,
            tracker=tracker,
            expected_output=(
                BackendOutputChunk(stream="stdout", data=b"hello\n"),
            ),
            expected_exit=BackendExit(exit_code=exit_code),
            register_resource=self._registrar(tracker),
        )

    async def make_failing_start_case(
        self,
        *,
        cancelled: bool,
    ) -> BackendContractCase:
        tracker = BackendContractTracker()
        source = CancellationSource()
        if cancelled:
            source.cancel("contract cancellation")
        request = self._request("[Console]::Out.Write('unreachable')")
        missing_shell = DiscoveredProgram(
            name="missing-powershell.exe",
            path=self.workspace / "missing-powershell.exe",
            shell_kind="powershell",
            version="contract",
        )
        failing_backend = WindowsBackend(
            self.descriptor,
            shells=(missing_shell,),
            host_id="contract-host",
        )
        return BackendContractCase(
            backend=TrackingBackend(failing_backend, tracker),
            prepared=self._prepared(request),
            request=request,
            context=self._context("failure"),
            cancellation=source.token,
            tracker=tracker,
            expected_output=(),
            expected_exit=BackendExit(exit_code=0),
            register_resource=self._registrar(tracker),
        )

    def _request(self, script: str) -> ExecutionRequest:
        return ExecutionRequest(
            mode="shell",
            argv=None,
            script=script,
            working_directory=self.workspace,
            limits=ExecutionLimits(
                timeout_seconds=30,
                max_output_bytes=4096,
                max_return_bytes=2048,
                memory_bytes=256 * 1024 * 1024,
                max_processes=16,
                termination_grace_seconds=0.1,
            ),
            network_access=False,
            filesystem_mode="host",
        )

    def _prepared(self, request: ExecutionRequest) -> PreparedExecution:
        return PreparedExecution(
            request=request,
            backend=self.descriptor,
            environment=construct_environment(
                platform="windows",
                inherited=os.environ,
                requested=(),
            ),
            resolved_shell="powershell",
        )

    def _context(self, suffix: str) -> BackendStartContext:
        name = self._testMethodName.replace("_", "-")
        return BackendStartContext(
            execution=ExecutionContext(
                execution_id=f"exec-contract-{name}-{suffix}",
                tool_call_id=f"call-contract-{name}-{suffix}",
                session_id="session-contract",
                turn_id="turn-contract",
                workspace_id="workspace-contract",
                project_root=self.workspace,
                launched_at_utc=datetime.now(UTC),
            ),
            audit_run_id=f"run-contract-{name}-{suffix}",
        )

    @staticmethod
    def _registrar(tracker: BackendContractTracker):
        async def register(resource) -> None:
            tracker.resource_registrations += 1
            tracker.registered_resource = resource
            tracker.lifecycle_events.append("registered")

        return register


if __name__ == "__main__":
    unittest.main()
