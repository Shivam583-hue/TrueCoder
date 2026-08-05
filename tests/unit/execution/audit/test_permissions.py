from __future__ import annotations

import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers.platforms import requires_posix_permissions, requires_symlinks
from truecoder.execution.audit.permissions import AuditPermissions
from truecoder.execution.errors import AuditUnavailableError


class AuditPermissionsTests(unittest.TestCase):
    @requires_posix_permissions
    def test_posix_storage_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "audit.sqlite3"
            permissions = AuditPermissions(platform="posix")

            permissions.prepare(path)

            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @requires_symlinks
    def test_rejects_database_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.touch()
            path = root / "audit.sqlite3"
            path.symlink_to(target)

            with self.assertRaises(AuditUnavailableError):
                AuditPermissions(platform="posix").prepare(path)

    def test_windows_uses_current_user_and_system_only_acls(self):
        calls: list[tuple[str, ...]] = []

        def run(arguments):
            calls.append(tuple(arguments))
            if arguments[0] == "whoami.exe":
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    '"DOMAIN\\\\user","S-1-5-21-1000"\n',
                    "",
                )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "audit.sqlite3"
            AuditPermissions(platform="nt", command_runner=run).prepare(path)

        icacls_calls = [call for call in calls if call[0] == "icacls.exe"]
        self.assertEqual(len(icacls_calls), 2)
        self.assertTrue(
            all("/inheritance:r" in arguments for arguments in icacls_calls)
        )
        self.assertTrue(
            all(
                any("S-1-5-21-1000" in argument for argument in arguments)
                for arguments in icacls_calls
            )
        )

    def test_windows_acl_failure_fails_closed(self):
        def run(arguments):
            if arguments[0] == "whoami.exe":
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    '"user","S-1-5-21-1000"\n',
                    "",
                )
            return subprocess.CompletedProcess(arguments, 5, "", "access denied")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.sqlite3"
            with self.assertRaises(AuditUnavailableError):
                AuditPermissions(platform="nt", command_runner=run).prepare(path)


if __name__ == "__main__":
    unittest.main()
