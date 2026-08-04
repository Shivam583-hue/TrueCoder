from __future__ import annotations

from tests.helpers.platforms import skip_module_on_windows

skip_module_on_windows('POSIX resource limits')

import unittest

from truecoder.execution.backends.posix_limits import build_rlimit_settings
from truecoder.execution.backends.posix_platform import (
    POSIX_PLATFORMS,
    PosixPlatformProfile,
    profile_for,
)
from truecoder.execution.discovery import _posix_capabilities
from truecoder.execution.models import ExecutionLimits


def limits(**overrides) -> ExecutionLimits:
    values = {
        "timeout_seconds": 10,
        "max_output_bytes": 1024,
        "max_return_bytes": 512,
        "memory_bytes": 1024 * 1024,
        "cpu_seconds": 5,
        "max_processes": 8,
        **overrides,
    }
    return ExecutionLimits(**values)


class PlatformProfileTests(unittest.TestCase):
    def test_every_posix_platform_has_a_profile(self):
        for system in POSIX_PLATFORMS:
            self.assertIsInstance(profile_for(system), PosixPlatformProfile)

    def test_an_unsupported_platform_is_refused(self):
        with self.assertRaises(ValueError):
            profile_for("windows")
        with self.assertRaises(ValueError):
            profile_for("plan9")

    def test_linux_supports_cgroups_and_macos_does_not(self):
        self.assertTrue(profile_for("linux").supports_cgroups)
        self.assertFalse(profile_for("macos").supports_cgroups)

    def test_macos_process_limit_is_per_user(self):
        self.assertTrue(profile_for("macos").process_limit_is_per_user)
        self.assertFalse(profile_for("linux").process_limit_is_per_user)

    def test_only_linux_can_prove_resource_ownership_after_restart(self):
        self.assertTrue(profile_for("linux").can_prove_resource_ownership)
        self.assertFalse(profile_for("macos").can_prove_resource_ownership)

    def test_macos_reports_its_unsupported_guarantees_explicitly(self):
        reasons = profile_for("macos").unsupported_reasons()

        self.assertIn("cgroup-controllers-unavailable", reasons)
        self.assertIn("process-limit-is-per-user", reasons)
        self.assertIn("resource-ownership-unprovable-after-restart", reasons)

    def test_linux_reports_no_unsupported_guarantees(self):
        self.assertEqual(profile_for("linux").unsupported_reasons(), ())


class ProcessLimitTests(unittest.TestCase):
    def test_linux_applies_a_process_rlimit(self):
        names = [
            setting.name
            for setting in build_rlimit_settings(limits(), profile_for("linux"))
        ]

        self.assertIn("processes", names)

    def test_macos_never_applies_a_per_user_process_rlimit(self):
        names = [
            setting.name
            for setting in build_rlimit_settings(limits(), profile_for("macos"))
        ]

        self.assertNotIn("processes", names)
        self.assertIn("memory", names)
        self.assertIn("cpu", names)

    def test_an_invalid_profile_is_refused(self):
        with self.assertRaises(TypeError):
            build_rlimit_settings(limits(), profile="macos")


class PlatformCapabilityTests(unittest.TestCase):
    def test_macos_reports_process_limits_as_unsupported(self):
        capabilities = _posix_capabilities(
            shell_kinds=("posix",),
            cgroup_v2=None,
            system="macos",
        )

        self.assertEqual(capabilities.process_limits, "unsupported")

    def test_linux_without_controllers_stays_best_effort(self):
        capabilities = _posix_capabilities(
            shell_kinds=("posix",),
            cgroup_v2=None,
            system="linux",
        )

        self.assertEqual(capabilities.process_limits, "best_effort")

    def test_no_posix_platform_claims_filesystem_or_network_isolation(self):
        for system in ("linux", "macos"):
            capabilities = _posix_capabilities(
                shell_kinds=("posix",),
                cgroup_v2=None,
                system=system,
            )
            self.assertEqual(capabilities.filesystem_isolation, "unsupported")
            self.assertEqual(capabilities.network_isolation, "unsupported")
            self.assertEqual(capabilities.supported_filesystem_modes, ("host",))


if __name__ == "__main__":
    unittest.main()
