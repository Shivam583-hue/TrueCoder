"""Risk must describe the command, not the request shape every command shares."""

from __future__ import annotations

import unittest
from pathlib import Path

from truecoder.execution.configuration import default_policy_config
from truecoder.execution.defaults import DEFAULT_EXECUTION_LIMITS
from truecoder.execution.models import ExecutionRequest, RiskLevel
from truecoder.execution.policy import evaluate_policy

ROOT = Path.cwd()


def _decide(argv: tuple[str, ...], **overrides):
    values = {
        "mode": "exec",
        "argv": argv,
        "script": None,
        "working_directory": ROOT,
        "limits": DEFAULT_EXECUTION_LIMITS,
        "network_access": True,
        "filesystem_mode": "host",
        "backend": "auto",
        "shell_kind": "auto",
    }
    values.update(overrides)
    return evaluate_policy(
        ExecutionRequest(**values),  # type: ignore[arg-type]
        default_policy_config(),
    )


class RiskDiscriminationTests(unittest.TestCase):
    def test_a_read_only_command_is_low_under_the_defaults(self):
        for argv in (("ls",), ("cat", "README.md"), ("git", "status")):
            with self.subTest(argv=argv):
                self.assertIs(_decide(argv).risk, RiskLevel.LOW)

    def test_a_dangerous_command_stays_above_a_harmless_one(self):
        harmless = _decide(("ls",)).risk
        dangerous = _decide(("rm", "-rf", "build")).risk

        self.assertIsNot(harmless, dangerous)
        self.assertIs(dangerous, RiskLevel.HIGH)

    def test_each_class_of_command_lands_where_it_should(self):
        cases = {
            ("pytest", "-q"): RiskLevel.LOW,
            ("pip", "install", "requests"): RiskLevel.MEDIUM,
            ("curl", "https://example.invalid"): RiskLevel.HIGH,
            ("chmod", "777", "."): RiskLevel.HIGH,
            ("git", "reset", "--hard"): RiskLevel.HIGH,
            ("sudo", "rm"): RiskLevel.CRITICAL,
        }

        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertIs(_decide(argv).risk, expected)

    def test_the_default_shape_never_decides_the_risk_alone(self):
        harmless = _decide(("ls",))

        codes = {reason.code for reason in harmless.reasons}
        self.assertIn("host-filesystem", codes)
        self.assertIn("network-enabled", codes)
        self.assertIs(harmless.risk, RiskLevel.LOW)

    def test_approval_is_still_required_for_a_low_risk_command(self):
        self.assertTrue(_decide(("ls",)).requires_approval)

    def test_the_access_a_command_was_granted_is_still_reported(self):
        without_network = _decide(("ls",), network_access=False)

        codes = {reason.code for reason in without_network.reasons}
        self.assertNotIn("network-enabled", codes)

    def test_a_denied_command_is_still_denied(self):
        decision = _decide(("sudo", "rm", "-rf", "/"))

        self.assertFalse(decision.allowed)


if __name__ == "__main__":
    unittest.main()
