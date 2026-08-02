from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from tests.contract.execution.backend_contract import (
    BackendContractCase,
    BackendContractMixin,
    BackendContractTestCase,
    BackendContractTracker,
)
from truecoder.execution.audit.models import BackendResourceIdentifier
from truecoder.execution.backends.base import ExecutionHandle
from truecoder.execution.backends.models import (
    BackendDescriptor,
    BackendExit,
    BackendOutputChunk,
    CleanupResult,
)
from truecoder.execution.cancellation import (
    CancellationRequested,
    CancellationSource,
    CancellationToken,
)
from truecoder.execution.errors import BackendStartError
from truecoder.execution.models import (
    BackendCapabilities,
    ExecutionContext,
    ExecutionLimits,
    ExecutionRequest,
    TerminationReason,
)

ROOT = Path.cwd().resolve()
NOW = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)


class FakeExecutionHandle:
    def __init__(
        self,
        *,
        context: ExecutionContext,
        tracker: BackendContractTracker,
        output: tuple[BackendOutputChunk, ...],
        exit_code: int,
    ) -> None:
        self._execution_id = context.execution_id
        self._tracker = tracker
        self._output = output
        self._exit_code = exit_code
        self._output_claimed = False
        self._termination_reason: TerminationReason | None = None
        self._exit: BackendExit | None = None
        self._cleanup: CleanupResult | None = None
        self._resource = BackendResourceIdentifier(
            version=1,
            backend="posix",
            resource_kind="fake-process-group",
            resource_id=context.execution_id,
            ownership_token=f"owner-{context.execution_id}",
            host_id="fake-host",
            created_at_utc=NOW,
            native_details=(("fake", "true"),),
        )

    @property
    def execution_id(self) -> str:
        return self._execution_id

    @property
    def resource(self) -> BackendResourceIdentifier:
        return self._resource

    def output(self) -> AsyncIterator[BackendOutputChunk]:
        if self._output_claimed:
            raise RuntimeError("output already has an owner")
        self._output_claimed = True

        async def iterate() -> AsyncIterator[BackendOutputChunk]:
            for chunk in self._output:
                yield chunk

        return iterate()

    async def wait(self) -> BackendExit:
        if self._exit is None:
            self._tracker.native_waits += 1
            self._exit = (
                BackendExit(
                    exit_code=None,
                    native_reason=self._termination_reason,
                )
                if self._termination_reason is not None
                else BackendExit(exit_code=self._exit_code)
            )
        return self._exit

    async def terminate(
        self,
        reason: TerminationReason,
        grace_seconds: float,
    ) -> None:
        if self._termination_reason is not None:
            return
        if grace_seconds < 0:
            raise ValueError("grace_seconds must not be negative")
        self._tracker.native_terminations += 1
        self._termination_reason = reason

    async def cleanup(self) -> CleanupResult:
        if self._cleanup is None:
            self._tracker.native_cleanups += 1
            self._tracker.live_resources -= 1
            self._cleanup = CleanupResult(complete=True)
        return self._cleanup


class FakeBackend:
    def __init__(
        self,
        *,
        tracker: BackendContractTracker,
        exit_code: int,
        output: tuple[BackendOutputChunk, ...],
        fail_after_acquire: bool = False,
    ) -> None:
        self._tracker = tracker
        self._exit_code = exit_code
        self._output = output
        self._fail_after_acquire = fail_after_acquire
        self._descriptor = BackendDescriptor(
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
                supported_execution_modes=("exec",),
                supported_filesystem_modes=("host",),
                supported_shells=(),
            ),
            version="fake-v1",
        )

    @property
    def descriptor(self) -> BackendDescriptor:
        return self._descriptor

    async def start(
        self,
        request: ExecutionRequest,
        context: ExecutionContext,
        cancellation: CancellationToken,
    ) -> ExecutionHandle:
        del request
        cancellation.raise_if_cancelled()
        self._tracker.live_resources += 1
        if self._fail_after_acquire:
            self._tracker.partial_start_cleanups += 1
            self._tracker.live_resources -= 1
            raise BackendStartError(
                "injected partial start failure",
                execution_id=context.execution_id,
                backend="posix",
                operation="start",
            )
        return FakeExecutionHandle(
            context=context,
            tracker=self._tracker,
            output=self._output,
            exit_code=self._exit_code,
        )


class FakeBackendContractTests(
    BackendContractMixin,
    BackendContractTestCase,
):
    async def make_backend_case(
        self,
        *,
        exit_code: int = 0,
    ) -> BackendContractCase:
        tracker = BackendContractTracker()
        output = (
            BackendOutputChunk(stream="stdout", data=b"hello\n"),
            BackendOutputChunk(stream="stderr", data=b"warning\n"),
        )
        return BackendContractCase(
            backend=FakeBackend(
                tracker=tracker,
                exit_code=exit_code,
                output=output,
            ),
            request=_request(),
            context=_context(),
            cancellation=CancellationSource().token,
            tracker=tracker,
            expected_output=output,
            expected_exit=BackendExit(exit_code=exit_code),
        )

    async def make_failing_start_case(
        self,
        *,
        cancelled: bool,
    ) -> BackendContractCase:
        tracker = BackendContractTracker()
        source = CancellationSource()
        if cancelled:
            source.cancel("test cancellation")
        return BackendContractCase(
            backend=FakeBackend(
                tracker=tracker,
                exit_code=0,
                output=(),
                fail_after_acquire=not cancelled,
            ),
            request=_request(),
            context=_context(),
            cancellation=source.token,
            tracker=tracker,
            expected_output=(),
            expected_exit=BackendExit(exit_code=0),
        )

    async def test_cancellation_uses_the_domain_exception(self):
        case = await self.make_failing_start_case(cancelled=True)

        with self.assertRaises(CancellationRequested):
            await case.backend.start(
                case.request,
                case.context,
                case.cancellation,
            )


def _request() -> ExecutionRequest:
    return ExecutionRequest(
        mode="exec",
        argv=("fake",),
        script=None,
        working_directory=ROOT,
        limits=ExecutionLimits(
            timeout_seconds=5,
            max_output_bytes=1024,
            max_return_bytes=512,
        ),
        network_access=True,
        filesystem_mode="host",
    )


def _context() -> ExecutionContext:
    return ExecutionContext(
        execution_id="exec_contract",
        tool_call_id="call_contract",
        session_id="session_contract",
        turn_id="turn_contract",
        workspace_id="workspace_contract",
        project_root=ROOT,
        launched_at_utc=NOW,
    )
