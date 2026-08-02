from __future__ import annotations

import asyncio
import os
import sys
import unittest

from tests.integration.execution.backends.test_posix_backend import (
    HELPERS,
    _backend,
    _context,
    _registrar,
    _request,
)
from truecoder.execution.audit.recovery import RecoveryDisposition
from truecoder.execution.backends.posix_recovery import PosixRecoveryHandler
from truecoder.execution.cancellation import CancellationSource


@unittest.skipUnless(os.name == "posix", "requires POSIX process semantics")
class PosixRecoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_terminates_a_live_exact_resource(self):
        handle = await _backend().start(
            _request(
                (
                    sys.executable,
                    str(HELPERS / "ignore_term.py"),
                )
            ),
            _context("exec_live_recovery"),
            CancellationSource().token,
            _registrar([]),
        )
        output_task = asyncio.create_task(_drain(handle))
        await asyncio.sleep(0.05)

        result = await PosixRecoveryHandler(
            termination_grace_seconds=0.5,
            poll_seconds=0.005,
        ).recover(handle.resource)

        self.assertIs(result, RecoveryDisposition.TERMINATED)
        self.assertEqual((await handle.wait()).native_reason, "shutdown")
        await output_task
        self.assertTrue((await handle.cleanup()).complete)


async def _drain(handle) -> None:
    async for _chunk in handle.output():
        pass


if __name__ == "__main__":
    unittest.main()
