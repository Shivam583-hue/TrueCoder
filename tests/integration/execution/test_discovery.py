from __future__ import annotations

import unittest

from truecoder.execution.discovery import discover_execution_environment
from truecoder.execution.models import BACKEND_NAMES


class HostDiscoverySmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_host_snapshot_is_internally_consistent(self):
        snapshot = await discover_execution_environment()

        self.assertEqual(
            frozenset(backend.name for backend in snapshot.backends),
            BACKEND_NAMES,
        )
        for shell in snapshot.shells:
            with self.subTest(shell=shell.name):
                self.assertTrue(shell.path.is_absolute())
                self.assertTrue(shell.path.exists())
        for runtime in snapshot.runtimes:
            with self.subTest(runtime=runtime.name):
                self.assertTrue(runtime.executable.is_absolute())
                self.assertTrue(runtime.executable.exists())
                if not runtime.daemon_reachable:
                    self.assertIsNone(runtime.server_version)
                    self.assertEqual(runtime.rootless, "unknown")
        for backend in snapshot.backends:
            with self.subTest(backend=backend.name):
                self.assertEqual(
                    backend.available,
                    not backend.unavailable_reasons,
                )
                if backend.name == "container" and backend.available:
                    self.assertIsNotNone(backend.runtime)


if __name__ == "__main__":
    unittest.main()
