from __future__ import annotations

import math
import resource
import unittest
from unittest.mock import patch

from truecoder.execution.backends.posix_limits import (
    RlimitSetting,
    apply_rlimit_settings,
    build_rlimit_settings,
)
from truecoder.execution.models import ExecutionLimits


class PosixLimitTests(unittest.TestCase):
    def test_builds_supported_limits_and_rounds_cpu_up(self):
        settings = build_rlimit_settings(
            ExecutionLimits(
                timeout_seconds=10,
                max_output_bytes=100,
                max_return_bytes=50,
                memory_bytes=2048,
                cpu_seconds=1.2,
                max_processes=4,
            )
        )
        values = {setting.name: setting.value for setting in settings}

        if hasattr(resource, "RLIMIT_AS"):
            self.assertEqual(values["memory"], 2048)
        if hasattr(resource, "RLIMIT_CPU"):
            self.assertEqual(values["cpu"], math.ceil(1.2))
        if hasattr(resource, "RLIMIT_NPROC"):
            self.assertEqual(values["processes"], 4)

    def test_apply_never_weakens_a_lower_inherited_limit(self):
        setting = RlimitSetting(name="cpu", resource_id=1, value=20)
        with (
            patch(
                "truecoder.execution.backends.posix_limits.resource.getrlimit",
                return_value=(5, 10),
            ),
            patch(
                "truecoder.execution.backends.posix_limits.resource.setrlimit"
            ) as set_limit,
        ):
            applied = apply_rlimit_settings((setting,))

        set_limit.assert_called_once_with(1, (5, 10))
        self.assertEqual((applied[0].soft, applied[0].hard), (5, 10))

    def test_apply_replaces_infinity_with_requested_limit(self):
        setting = RlimitSetting(name="memory", resource_id=2, value=4096)
        with (
            patch(
                "truecoder.execution.backends.posix_limits.resource.getrlimit",
                return_value=(resource.RLIM_INFINITY, resource.RLIM_INFINITY),
            ),
            patch(
                "truecoder.execution.backends.posix_limits.resource.setrlimit"
            ) as set_limit,
        ):
            apply_rlimit_settings((setting,))

        set_limit.assert_called_once_with(2, (4096, 4096))


if __name__ == "__main__":
    unittest.main()
