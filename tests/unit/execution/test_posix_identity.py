from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from truecoder.execution.backends.posix_identity import (
    PosixProcessFacts,
    create_posix_resource,
    resource_native_details,
    verify_posix_resource,
)
from truecoder.execution.models import ExecutionContext

ROOT = Path.cwd().resolve()
NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)


def _context() -> ExecutionContext:
    return ExecutionContext(
        execution_id="exec_identity",
        tool_call_id="call_identity",
        session_id="session_identity",
        turn_id="turn_identity",
        workspace_id="workspace_identity",
        project_root=ROOT,
        launched_at_utc=NOW,
    )


def _facts(
    *,
    start_ticks: int = 500,
    session_id: int = 100,
) -> PosixProcessFacts:
    return PosixProcessFacts(
        pid=100,
        process_group_id=100,
        session_id=session_id,
        start_ticks=start_ticks,
        state="S",
    )


class PosixIdentityTests(unittest.TestCase):
    def _resource(self):
        with (
            patch(
                "truecoder.execution.backends.posix_identity.read_process_facts",
                return_value=_facts(),
            ),
            patch(
                "truecoder.execution.backends.posix_identity.current_host_id",
                return_value="host-one",
            ),
            patch(
                "truecoder.execution.backends.posix_identity.current_boot_id",
                return_value="boot-one",
            ),
        ):
            return create_posix_resource(
                _context(),
                supervisor_pid=100,
                project_pgid=101,
                ownership_token="owner-one",
                cgroup_path=ROOT / "cgroup",
            )

    def test_resource_contains_exact_recovery_identity(self):
        resource = self._resource()
        details = resource_native_details(resource)

        self.assertEqual(resource.resource_id, "exec_identity")
        self.assertEqual(details["supervisor_pid"], "100")
        self.assertEqual(details["project_pgid"], "101")
        self.assertEqual(details["supervisor_start_ticks"], "500")
        self.assertEqual(details["boot_id"], "boot-one")
        self.assertEqual(details["cgroup_path"], str(ROOT / "cgroup"))

    def test_linux_verification_requires_every_identity_fact(self):
        resource = self._resource()
        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "truecoder.execution.backends.posix_identity.current_host_id",
                return_value="host-one",
            ),
            patch(
                "truecoder.execution.backends.posix_identity.current_boot_id",
                return_value="boot-one",
            ),
            patch(
                "truecoder.execution.backends.posix_identity.read_process_facts",
                return_value=_facts(),
            ),
            patch(
                "truecoder.execution.backends.posix_identity.process_group_exists",
                return_value=True,
            ),
        ):
            self.assertTrue(verify_posix_resource(resource).matches)

        mismatch_cases = (
            ("host-mismatch", "current_host_id", "host-two"),
            ("boot-mismatch", "current_boot_id", "boot-two"),
        )
        for expected, target, value in mismatch_cases:
            with (
                self.subTest(expected=expected),
                patch("platform.system", return_value="Linux"),
                patch(
                    "truecoder.execution.backends.posix_identity.current_host_id",
                    return_value=(
                        value if target == "current_host_id" else "host-one"
                    ),
                ),
                patch(
                    "truecoder.execution.backends.posix_identity.current_boot_id",
                    return_value=(
                        value if target == "current_boot_id" else "boot-one"
                    ),
                ),
                patch(
                    "truecoder.execution.backends.posix_identity.read_process_facts",
                    return_value=_facts(),
                ),
            ):
                self.assertEqual(verify_posix_resource(resource).reason, expected)

    def test_pid_reuse_is_rejected_by_start_ticks(self):
        resource = self._resource()
        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "truecoder.execution.backends.posix_identity.current_host_id",
                return_value="host-one",
            ),
            patch(
                "truecoder.execution.backends.posix_identity.current_boot_id",
                return_value="boot-one",
            ),
            patch(
                "truecoder.execution.backends.posix_identity.read_process_facts",
                return_value=_facts(start_ticks=999),
            ),
        ):
            result = verify_posix_resource(resource)

        self.assertFalse(result.matches)
        self.assertEqual(result.reason, "start-time-mismatch")

    def test_absent_supervisor_is_distinct_from_identity_mismatch(self):
        resource = self._resource()
        with (
            patch(
                "truecoder.execution.backends.posix_identity.current_host_id",
                return_value="host-one",
            ),
            patch(
                "truecoder.execution.backends.posix_identity.read_process_facts",
                return_value=None,
            ),
        ):
            result = verify_posix_resource(resource)

        self.assertTrue(result.resource_absent)
        self.assertEqual(result.reason, "supervisor-absent")

    def test_macos_live_pid_is_not_claimed_recoverable(self):
        resource = self._resource()
        with (
            patch("platform.system", return_value="Darwin"),
            patch(
                "truecoder.execution.backends.posix_identity.current_host_id",
                return_value="host-one",
            ),
            patch(
                "truecoder.execution.backends.posix_identity.read_process_facts",
                return_value=_facts(start_ticks=None),
            ),
        ):
            result = verify_posix_resource(resource)

        self.assertFalse(result.matches)
        self.assertEqual(result.reason, "ownership-unverifiable")


if __name__ == "__main__":
    unittest.main()
