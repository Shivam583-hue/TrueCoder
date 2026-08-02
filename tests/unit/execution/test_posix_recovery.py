from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from truecoder.execution.audit.models import BackendResourceIdentifier
from truecoder.execution.audit.recovery import RecoveryDisposition
from truecoder.execution.backends.posix_identity import PosixIdentityVerification
from truecoder.execution.backends.posix_recovery import PosixRecoveryHandler
from truecoder.execution.errors import AuditRecoveryError


def _resource() -> BackendResourceIdentifier:
    return BackendResourceIdentifier(
        version=1,
        backend="posix",
        resource_kind="supervised-process-group",
        resource_id="exec_recovery",
        ownership_token="owner-recovery",
        host_id="host-recovery",
        created_at_utc=datetime(2026, 8, 2, tzinfo=timezone.utc),
        native_details=(
            ("supervisor_pid", "100"),
            ("project_pgid", "101"),
            ("protocol_version", "1"),
            ("boot_id", "boot"),
            ("supervisor_start_ticks", "50"),
        ),
    )


class PosixRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_absent_resource_is_not_signalled(self):
        handler = PosixRecoveryHandler(poll_seconds=0.001)
        with (
            patch(
                "truecoder.execution.backends.posix_recovery.verify_posix_resource",
                return_value=PosixIdentityVerification(
                    matches=False,
                    resource_absent=True,
                    reason="supervisor-absent",
                ),
            ),
            patch(
                "truecoder.execution.backends.posix_recovery.os.kill"
            ) as kill,
        ):
            result = await handler.recover(_resource())

        self.assertIs(result, RecoveryDisposition.RESOURCE_ABSENT)
        kill.assert_not_called()

    async def test_exact_resource_is_terminated(self):
        handler = PosixRecoveryHandler(poll_seconds=0.001)
        process_states = iter((True, False))
        with (
            patch(
                "truecoder.execution.backends.posix_recovery.verify_posix_resource",
                return_value=PosixIdentityVerification(
                    matches=True,
                    resource_absent=False,
                    reason="exact-match",
                ),
            ),
            patch(
                "truecoder.execution.backends.posix_recovery.process_exists",
                side_effect=lambda _pid: next(process_states),
            ),
            patch(
                "truecoder.execution.backends.posix_recovery.process_group_exists",
                return_value=False,
            ),
            patch(
                "truecoder.execution.backends.posix_recovery.os.kill"
            ) as kill,
        ):
            result = await handler.recover(_resource())

        self.assertIs(result, RecoveryDisposition.TERMINATED)
        kill.assert_called_once()

    async def test_identity_mismatch_fails_closed(self):
        handler = PosixRecoveryHandler()
        with (
            patch(
                "truecoder.execution.backends.posix_recovery.verify_posix_resource",
                return_value=PosixIdentityVerification(
                    matches=False,
                    resource_absent=False,
                    reason="start-time-mismatch",
                ),
            ),
            patch(
                "truecoder.execution.backends.posix_recovery.os.kill"
            ) as kill,
            self.assertRaises(AuditRecoveryError),
        ):
            await handler.recover(_resource())

        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
