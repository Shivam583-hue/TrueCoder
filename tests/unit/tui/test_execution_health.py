from __future__ import annotations

import unittest

from truecoder.execution.bootstrap import (
    BackendHealth,
    ExecutionHealthReport,
)
from truecoder.tui.execution_health import (
    health_failure_message,
    health_lines,
)


def report(**overrides) -> ExecutionHealthReport:
    values = {
        "enabled": True,
        "audit_ready": True,
        "recovery_ready": True,
        "backends": (
            BackendHealth(
                name="posix",
                discovered=True,
                registered=True,
            ),
            BackendHealth(
                name="container",
                discovered=False,
                registered=False,
                reasons=("Docker daemon is unavailable.",),
            ),
        ),
        "failure_code": None,
        **overrides,
    }
    return ExecutionHealthReport(**values)


class ExecutionHealthViewTests(unittest.TestCase):
    def test_a_healthy_runtime_has_no_failure_message(self):
        self.assertIsNone(health_failure_message(report()))

    def test_failures_prefer_the_stable_bootstrap_code(self):
        unavailable = report(
            audit_ready=False,
            failure_code="audit_unavailable",
        )

        self.assertEqual(
            health_failure_message(unavailable),
            "audit unavailable",
        )

    def test_every_backend_and_reason_is_visible(self):
        lines = "\n".join(health_lines(report()))

        self.assertIn("posix      ready", lines)
        self.assertIn("container  not discovered", lines)
        self.assertIn("Docker daemon is unavailable.", lines)


if __name__ == "__main__":
    unittest.main()
