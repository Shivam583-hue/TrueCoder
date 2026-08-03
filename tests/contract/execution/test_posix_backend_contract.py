from __future__ import annotations

import os
import sys
import unittest
from collections.abc import AsyncIterator

from tests.contract.execution.backend_contract import (
    BackendContractCase,
    BackendContractMixin,
    BackendContractTestCase,
    BackendContractTracker,
)
from tests.integration.execution.backends.test_posix_backend import (
    HELPERS,
    _backend,
    _context,
    _prepared,
    _registrar,
    _request,
)
from truecoder.execution.audit.models import BackendResourceIdentifier
from truecoder.execution.backends.base import (
    BackendResourceRegistrar,
    ExecutionBackend,
    ExecutionHandle,
)
from truecoder.execution.backends.models import (
    BackendDescriptor,
    BackendExit,
    BackendOutputChunk,
    CleanupResult,
)
from truecoder.execution.cancellation import (
    CancellationSource,
    CancellationToken,
)
from truecoder.execution.models import (
    ExecutionContext,
    ExecutionRequest,
    TerminationReason,
)
from truecoder.execution.preparation import PreparedExecution


class TrackingHandle:
    def __init__(
        self,
        inner: ExecutionHandle,
        tracker: BackendContractTracker,
    ) -> None:
        self._inner = inner
        self._tracker = tracker
        self._waited = False
        self._terminated = False
        self._cleaned = False

    @property
    def execution_id(self) -> str:
        return self._inner.execution_id

    @property
    def resource(self) -> BackendResourceIdentifier:
        return self._inner.resource

    def output(self) -> AsyncIterator[BackendOutputChunk]:
        return self._inner.output()

    async def wait(self) -> BackendExit:
        if not self._waited:
            self._tracker.native_waits += 1
            self._waited = True
        return await self._inner.wait()

    async def terminate(
        self,
        reason: TerminationReason,
        grace_seconds: float,
    ) -> None:
        if not self._terminated:
            self._tracker.native_terminations += 1
            self._terminated = True
        await self._inner.terminate(reason, grace_seconds)

    async def cleanup(self) -> CleanupResult:
        if not self._cleaned:
            self._tracker.native_cleanups += 1
            self._tracker.live_resources -= 1
            self._cleaned = True
        return await self._inner.cleanup()


class TrackingBackend:
    def __init__(
        self,
        inner: ExecutionBackend,
        tracker: BackendContractTracker,
    ) -> None:
        self._inner = inner
        self._tracker = tracker

    @property
    def descriptor(self) -> BackendDescriptor:
        return self._inner.descriptor

    async def start(
        self,
        prepared: PreparedExecution,
        request: ExecutionRequest,
        context: ExecutionContext,
        cancellation: CancellationToken,
        register_resource: BackendResourceRegistrar,
    ) -> ExecutionHandle:
        try:
            handle = await self._inner.start(
                prepared,
                request,
                context,
                cancellation,
                register_resource,
            )
        except BaseException:
            if not cancellation.cancelled:
                self._tracker.partial_start_cleanups += 1
            raise
        self._tracker.live_resources += 1
        self._tracker.lifecycle_events.append("released")
        return TrackingHandle(handle, self._tracker)


@unittest.skipUnless(os.name == "posix", "requires POSIX process semantics")
class PosixBackendContractTests(
    BackendContractMixin,
    BackendContractTestCase,
):
    async def make_backend_case(
        self,
        *,
        exit_code: int = 0,
    ) -> BackendContractCase:
        tracker = BackendContractTracker()
        output = (BackendOutputChunk(stream="stdout", data=b"hello\n"),)

        async def register(resource) -> None:
            tracker.resource_registrations += 1
            tracker.registered_resource = resource
            tracker.lifecycle_events.append("registered")

        request = _request(
            (
                sys.executable,
                str(HELPERS / "emit_output.py"),
                "--stdout",
                "hello\n",
                "--exit-code",
                str(exit_code),
            )
        )
        return BackendContractCase(
            backend=TrackingBackend(_backend(), tracker),
            prepared=_prepared(request),
            request=request,
            context=_context("exec_contract_posix"),
            cancellation=CancellationSource().token,
            tracker=tracker,
            expected_output=output,
            expected_exit=BackendExit(exit_code=exit_code),
            register_resource=register,
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

        request = _request(("/truecoder/contract-missing-executable",))
        return BackendContractCase(
            backend=TrackingBackend(_backend(), tracker),
            prepared=_prepared(request),
            request=request,
            context=_context("exec_contract_failure"),
            cancellation=source.token,
            tracker=tracker,
            expected_output=(),
            expected_exit=BackendExit(exit_code=0),
            register_resource=_registrar([]),
        )
if __name__ == "__main__":
    unittest.main()
