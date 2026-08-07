"""Default shell arguments must be runnable on the machine the user is on."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from truecoder.execution.configuration import default_policy_config
from truecoder.execution.discovery import _posix_capabilities, _windows_capabilities
from truecoder.execution.policy import _base_requirements
from truecoder.execution.selection import capability_meets
from truecoder.tools.builtin.shell import (
    ShellArguments,
    ShellDefaults,
    build_shell_request,
)

CONTAINER_FILESYSTEM_MODES = ("workspace-read", "workspace-write")


class ShellDefaultsRunLocallyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)
        self.request = self._request()

    def _request(self, **overrides):
        return build_shell_request(
            ShellArguments(argv=("pytest", "-q"), **overrides),
            project_root=self.root,
            defaults=ShellDefaults(),
        )

    def _local_capabilities(self):
        return (
            (
                "posix",
                _posix_capabilities(shell_kinds=("posix",), cgroup_v2=None),
            ),
            (
                "windows",
                _windows_capabilities(shell_kinds=("powershell",)),
            ),
        )

    def test_the_default_filesystem_mode_is_one_a_local_backend_supports(self):
        for name, capabilities in self._local_capabilities():
            with self.subTest(backend=name):
                self.assertIn(
                    self.request.filesystem_mode,
                    capabilities.supported_filesystem_modes,
                )

    def test_the_defaults_demand_no_isolation_a_local_backend_lacks(self):
        requirements = _base_requirements(self.request, default_policy_config())

        for name, capabilities in self._local_capabilities():
            with self.subTest(backend=name):
                self.assertTrue(
                    capability_meets(
                        capabilities.filesystem_isolation,
                        requirements.filesystem_isolation,
                    )
                )
                self.assertTrue(
                    capability_meets(
                        capabilities.network_isolation,
                        requirements.network_isolation,
                    )
                )

    def test_a_local_backend_could_never_serve_the_old_defaults(self):
        request = self._request(
            filesystem_mode="workspace-read",
            network_access=False,
        )

        for name, capabilities in self._local_capabilities():
            with self.subTest(backend=name):
                self.assertNotIn(
                    request.filesystem_mode,
                    capabilities.supported_filesystem_modes,
                )

    def test_the_container_remains_reachable_by_asking_for_it(self):
        request = self._request(
            backend="container",
            filesystem_mode="workspace-read",
            network_access=False,
        )

        self.assertEqual(request.backend, "container")
        self.assertIn(request.filesystem_mode, CONTAINER_FILESYSTEM_MODES)


if __name__ == "__main__":
    unittest.main()
