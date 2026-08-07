"""A critical command policy still permits may not run against the host."""

from __future__ import annotations

import unittest
from pathlib import Path

from truecoder.execution.defaults import DEFAULT_EXECUTION_LIMITS
from truecoder.execution.discovery import _posix_capabilities
from truecoder.execution.models import ExecutionRequest, RiskLevel
from truecoder.execution.policy import PolicyConfig, evaluate_policy
from truecoder.execution.selection import capability_meets

ROOT = Path.cwd()


def _config(unknown_risk: RiskLevel = RiskLevel.MEDIUM) -> PolicyConfig:
    return PolicyConfig(
        version="test",
        limit_ceiling=DEFAULT_EXECUTION_LIMITS,
        unknown_risk=unknown_risk,
    )


def _request(**overrides) -> ExecutionRequest:
    values = {
        "mode": "exec",
        "argv": ("some-unrecognised-binary",),
        "script": None,
        "working_directory": ROOT,
        "limits": DEFAULT_EXECUTION_LIMITS,
        "network_access": True,
        "filesystem_mode": "host",
        "backend": "auto",
        "shell_kind": "auto",
    }
    values.update(overrides)
    return ExecutionRequest(**values)  # type: ignore[arg-type]


class CriticalIsolationTests(unittest.TestCase):
    def _critical(self, **overrides):
        return evaluate_policy(
            _request(**overrides),
            _config(RiskLevel.CRITICAL),
        )

    def test_a_permitted_critical_command_demands_isolation(self):
        decision = self._critical()

        self.assertTrue(decision.allowed)
        self.assertIs(decision.risk, RiskLevel.CRITICAL)
        self.assertEqual(decision.requirements.filesystem_isolation, "enforced")
        self.assertEqual(decision.requirements.network_isolation, "enforced")

    def test_the_reason_is_stated_to_the_user(self):
        decision = self._critical()

        codes = [reason.code for reason in decision.reasons]
        self.assertIn("critical-requires-isolation", codes)

    def test_no_local_backend_can_serve_a_critical_command(self):
        decision = self._critical()
        capabilities = _posix_capabilities(shell_kinds=("posix",), cgroup_v2=None)

        self.assertFalse(
            capability_meets(
                capabilities.filesystem_isolation,
                decision.requirements.filesystem_isolation,
            )
        )
        self.assertFalse(
            capability_meets(
                capabilities.network_isolation,
                decision.requirements.network_isolation,
            )
        )

    def test_an_isolated_backend_satisfies_the_escalated_requirements(self):
        decision = self._critical()

        for actual in ("filesystem_isolation", "network_isolation"):
            with self.subTest(capability=actual):
                self.assertTrue(
                    capability_meets(
                        "enforced",
                        getattr(decision.requirements, actual),
                    )
                )

    def test_an_ordinary_command_is_left_alone(self):
        decision = evaluate_policy(_request(argv=("ls",)), _config())

        self.assertIsNot(decision.risk, RiskLevel.CRITICAL)
        self.assertEqual(decision.requirements.filesystem_isolation, "none")
        self.assertEqual(decision.requirements.network_isolation, "none")

    def test_a_denied_command_is_not_given_requirements_instead(self):
        decision = evaluate_policy(
            _request(argv=("sudo", "rm", "-rf", "/")),
            _config(),
        )

        self.assertFalse(decision.allowed)
        codes = [reason.code for reason in decision.reasons]
        self.assertNotIn("critical-requires-isolation", codes)

    def test_escalation_follows_the_configured_minimum_isolation(self):
        config = PolicyConfig(
            version="test",
            limit_ceiling=DEFAULT_EXECUTION_LIMITS,
            unknown_risk=RiskLevel.CRITICAL,
            minimum_isolation="best_effort",
        )

        decision = evaluate_policy(_request(), config)

        self.assertEqual(decision.requirements.filesystem_isolation, "best_effort")

    def test_a_critical_command_already_asking_for_the_sandbox_still_runs(self):
        decision = self._critical(
            filesystem_mode="workspace-write",
            network_access=False,
            backend="container",
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.requirements.filesystem_isolation, "enforced")


if __name__ == "__main__":
    unittest.main()
