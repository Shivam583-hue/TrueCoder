"""With nobody watching, what may proceed is a configured decision."""

from __future__ import annotations

import unittest
from pathlib import Path

from truecoder.agent.autonomy import (
    Autonomy,
    UnattendedApprovals,
    autonomy_from_name,
    refusal_reason,
)
from truecoder.execution.approval import (
    ApprovalIdentity,
    ApprovalRequest,
    ExecutionApprovalDetails,
)
from truecoder.execution.defaults import DEFAULT_EXECUTION_LIMITS
from truecoder.execution.models import (
    BackendCapabilities,
    ExecutionRequest,
    RiskLevel,
)
from truecoder.mutation import build_file_diff

IDENTITY = ApprovalIdentity(session_id="session_1", workspace_id="workspace_1")

CAPABILITIES = BackendCapabilities(
    filesystem_isolation="unsupported",
    network_isolation="unsupported",
    memory_limits="best_effort",
    cpu_limits="best_effort",
    process_limits="best_effort",
    timeout_enforcement="enforced",
    cancellation="enforced",
    supported_execution_modes=("exec", "shell"),
    supported_filesystem_modes=("host",),
    supported_shells=("posix",),
)


def _execution(risk: RiskLevel) -> ExecutionApprovalDetails:
    return ExecutionApprovalDetails(
        execution_id="exec_1",
        command_display="ls",
        request=ExecutionRequest(
            mode="exec",
            argv=("ls",),
            script=None,
            working_directory=Path.cwd(),
            limits=DEFAULT_EXECUTION_LIMITS,
            network_access=True,
            filesystem_mode="host",
        ),
        backend="posix",
        capabilities=CAPABILITIES,
        risk=risk,
        reasons=("because",),
        policy_version="test",
    )


def _request(*, risk: RiskLevel | None = None, mutating: bool = False):
    return ApprovalRequest.create(
        call_id="call_1",
        tool_name="shell" if risk is not None else "write_file",
        arguments={"a": 1},
        identity=IDENTITY,
        execution=None if risk is None else _execution(risk),
        mutation=(
            build_file_diff("a.py", "old", "new", kind="replace") if mutating else None
        ),
    )


class AutonomyNameTests(unittest.TestCase):
    def test_every_level_parses(self):
        for level in Autonomy:
            self.assertIs(autonomy_from_name(level.value), level)

    def test_an_unknown_level_names_the_valid_ones(self):
        with self.assertRaises(ValueError) as caught:
            autonomy_from_name("yolo")

        self.assertIn("read-only", str(caught.exception))


class RefusalTests(unittest.TestCase):
    def test_read_only_refuses_every_command(self):
        for risk in RiskLevel:
            with self.subTest(risk=risk):
                reason = refusal_reason(_request(risk=risk), Autonomy.READ_ONLY)
                self.assertIsNotNone(reason)

    def test_read_only_refuses_a_file_change(self):
        self.assertIsNotNone(
            refusal_reason(_request(mutating=True), Autonomy.READ_ONLY)
        )

    def test_read_only_allows_a_plain_read(self):
        self.assertIsNone(refusal_reason(_request(), Autonomy.READ_ONLY))

    def test_edit_allows_a_file_change(self):
        self.assertIsNone(refusal_reason(_request(mutating=True), Autonomy.EDIT))

    def test_edit_allows_up_to_medium_and_refuses_above(self):
        self.assertIsNone(refusal_reason(_request(risk=RiskLevel.LOW), Autonomy.EDIT))
        self.assertIsNone(
            refusal_reason(_request(risk=RiskLevel.MEDIUM), Autonomy.EDIT)
        )
        self.assertIsNotNone(
            refusal_reason(_request(risk=RiskLevel.HIGH), Autonomy.EDIT)
        )

    def test_full_allows_high_and_still_refuses_critical(self):
        self.assertIsNone(refusal_reason(_request(risk=RiskLevel.HIGH), Autonomy.FULL))
        self.assertIsNotNone(
            refusal_reason(_request(risk=RiskLevel.CRITICAL), Autonomy.FULL)
        )

    def test_a_refusal_says_which_ceiling_was_exceeded(self):
        reason = refusal_reason(_request(risk=RiskLevel.HIGH), Autonomy.EDIT)

        self.assertIn("high", str(reason))
        self.assertIn("edit", str(reason))


class UnattendedApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_allowed_call_is_approved_once(self):
        handler = UnattendedApprovals(Autonomy.EDIT)

        response = await handler(_request(risk=RiskLevel.LOW))

        self.assertTrue(response.decision.value == "approved")
        self.assertEqual(handler.approved, ["shell"])

    async def test_a_refused_call_is_rejected_and_recorded(self):
        handler = UnattendedApprovals(Autonomy.EDIT)

        response = await handler(_request(risk=RiskLevel.HIGH))

        self.assertTrue(response.decision.value == "rejected")
        self.assertEqual(len(handler.refused), 1)
        self.assertEqual(handler.refused[0][0], "shell")

    async def test_the_default_is_the_most_restrictive_level(self):
        handler = UnattendedApprovals()

        self.assertIs(handler.autonomy, Autonomy.READ_ONLY)

    async def test_a_non_autonomy_is_rejected(self):
        with self.assertRaises(TypeError):
            UnattendedApprovals("full")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
