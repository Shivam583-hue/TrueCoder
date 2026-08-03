from __future__ import annotations

import os
import sys
import unittest

from tests.contract.execution.backend_contract import (
    BackendContractCase,
    BackendContractMixin,
    BackendContractTestCase,
    BackendContractTracker,
    TrackingBackend,
)
from tests.integration.execution.backends.test_posix_backend import (
    HELPERS,
    _backend,
    _context,
    _prepared,
    _registrar,
    _request,
)
from truecoder.execution.backends.models import (
    BackendExit,
    BackendOutputChunk,
)
from truecoder.execution.cancellation import CancellationSource


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
