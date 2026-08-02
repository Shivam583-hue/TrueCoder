from __future__ import annotations

import csv
import os
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from platformdirs import user_data_path

from truecoder.execution.errors import AuditUnavailableError

CommandResult = subprocess.CompletedProcess[str]
CommandRunner = Callable[[Sequence[str]], CommandResult]


def default_audit_database_path() -> Path:
    """Return the platform-native location for durable execution evidence."""

    return user_data_path("truecoder", appauthor=False) / "audit.sqlite3"


class AuditPermissions:
    """Create audit storage with private, platform-appropriate permissions."""

    def __init__(
        self,
        *,
        platform: str | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self._platform = platform or os.name
        if self._platform not in {"posix", "nt"}:
            raise AuditUnavailableError(
                f"unsupported audit storage platform: {self._platform}",
                operation="secure_audit_storage",
            )
        self._command_runner = command_runner or self._run_command

    def prepare(self, database_path: Path) -> None:
        if not isinstance(database_path, Path):
            raise TypeError("database_path must be a pathlib.Path")

        path = Path(os.path.abspath(database_path.expanduser()))
        self._reject_symlink(path.parent, "audit directory")
        self._reject_symlink(path, "audit database")

        try:
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
        except OSError as error:
            raise AuditUnavailableError(
                f"could not create private audit storage: {error}",
                operation="secure_audit_storage",
            ) from error

        if self._platform == "posix":
            self._secure_posix(path)
        else:
            self._secure_windows(path)

    def secure_sidecars(self, database_path: Path) -> None:
        """Secure SQLite WAL/shared-memory files after SQLite creates them."""

        if self._platform == "posix":
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{database_path}{suffix}")
                if sidecar.exists():
                    self._reject_symlink(sidecar, "audit database sidecar")
                    try:
                        sidecar.chmod(0o600)
                    except OSError as error:
                        raise AuditUnavailableError(
                            f"could not secure audit sidecar {sidecar.name}: {error}",
                            operation="secure_audit_storage",
                        ) from error
            return

        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database_path}{suffix}")
            if sidecar.exists():
                self._apply_windows_acl(sidecar, directory=False)

    @staticmethod
    def _reject_symlink(path: Path, description: str) -> None:
        try:
            if path.is_symlink():
                raise AuditUnavailableError(
                    f"{description} must not be a symbolic link",
                    operation="secure_audit_storage",
                )
        except OSError as error:
            raise AuditUnavailableError(
                f"could not inspect {description}: {error}",
                operation="secure_audit_storage",
            ) from error

    def _secure_posix(self, path: Path) -> None:
        try:
            path.parent.chmod(0o700)
            path.chmod(0o600)
            directory_mode = stat.S_IMODE(path.parent.stat().st_mode)
            database_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as error:
            raise AuditUnavailableError(
                f"could not set private audit permissions: {error}",
                operation="secure_audit_storage",
            ) from error

        if directory_mode != 0o700 or database_mode != 0o600:
            raise AuditUnavailableError(
                "audit storage permissions could not be restricted",
                operation="secure_audit_storage",
            )

    def _secure_windows(self, path: Path) -> None:
        sid = self._windows_user_sid()
        self._apply_windows_acl(path.parent, directory=True, sid=sid)
        self._apply_windows_acl(path, directory=False, sid=sid)

    def _windows_user_sid(self) -> str:
        result = self._command_runner(("whoami.exe", "/user", "/fo", "csv", "/nh"))
        if result.returncode != 0:
            raise AuditUnavailableError(
                "could not determine the current Windows security identifier",
                operation="secure_audit_storage",
            )
        try:
            row = next(csv.reader([result.stdout.strip()]))
            sid = row[1].strip()
        except (IndexError, StopIteration) as error:
            raise AuditUnavailableError(
                "Windows returned an invalid security identifier",
                operation="secure_audit_storage",
            ) from error
        if not sid.startswith("S-"):
            raise AuditUnavailableError(
                "Windows returned an invalid security identifier",
                operation="secure_audit_storage",
            )
        return sid

    def _apply_windows_acl(
        self,
        path: Path,
        *,
        directory: bool,
        sid: str | None = None,
    ) -> None:
        user_sid = sid or self._windows_user_sid()
        inheritance = "(OI)(CI)" if directory else ""
        result = self._command_runner(
            (
                "icacls.exe",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"*{user_sid}:{inheritance}F",
                f"*S-1-5-18:{inheritance}F",
            )
        )
        if result.returncode != 0:
            raise AuditUnavailableError(
                f"could not restrict Windows ACLs for {path}",
                operation="secure_audit_storage",
            )

    @staticmethod
    def _run_command(arguments: Sequence[str]) -> CommandResult:
        try:
            return subprocess.run(
                tuple(arguments),
                capture_output=True,
                check=False,
                text=True,
            )
        except OSError as error:
            raise AuditUnavailableError(
                f"could not apply Windows audit permissions: {error}",
                operation="secure_audit_storage",
            ) from error
