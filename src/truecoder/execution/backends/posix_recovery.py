from __future__ import annotations

import asyncio
import hashlib
import os
import signal
from contextlib import suppress
from pathlib import Path

from ..audit.models import BackendResourceIdentifier
from ..audit.recovery import RecoveryDisposition
from ..errors import AuditRecoveryError
from .posix_identity import (
    process_exists,
    process_group_exists,
    resource_native_details,
    verify_posix_resource,
)


class PosixRecoveryHandler:
    def __init__(
        self,
        *,
        termination_grace_seconds: float = 1.0,
        poll_seconds: float = 0.025,
    ) -> None:
        if (
            isinstance(termination_grace_seconds, bool)
            or not isinstance(termination_grace_seconds, (int, float))
            or termination_grace_seconds < 0
        ):
            raise ValueError("termination_grace_seconds must not be negative")
        if (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or poll_seconds <= 0
        ):
            raise ValueError("poll_seconds must be positive")
        self._grace = float(termination_grace_seconds)
        self._poll = float(poll_seconds)

    async def recover(
        self,
        resource: BackendResourceIdentifier,
    ) -> RecoveryDisposition:
        verification = verify_posix_resource(resource)
        if verification.resource_absent:
            await self._cleanup_owned_cgroup(resource)
            return RecoveryDisposition.RESOURCE_ABSENT
        if not verification.matches:
            raise AuditRecoveryError(
                f"POSIX recovery identity failed: {verification.reason}",
                execution_id=resource.resource_id,
                backend="posix",
                operation="recover",
            )
        details = resource_native_details(resource)
        supervisor_pid = _positive_int(details, "supervisor_pid")
        project_pgid = _positive_int(details, "project_pgid")

        with suppress(ProcessLookupError):
            os.kill(supervisor_pid, signal.SIGTERM)
        if not await self._wait_process_absent(supervisor_pid, self._grace):
            with suppress(ProcessLookupError):
                os.killpg(project_pgid, signal.SIGKILL)
            with suppress(ProcessLookupError):
                os.kill(supervisor_pid, signal.SIGKILL)
            if not await self._wait_process_absent(supervisor_pid, 1.0):
                raise AuditRecoveryError(
                    "POSIX supervisor survived recovery termination",
                    execution_id=resource.resource_id,
                    backend="posix",
                    operation="recover",
                )
        if process_group_exists(project_pgid):
            with suppress(ProcessLookupError):
                os.killpg(project_pgid, signal.SIGKILL)
            if not await self._wait_group_absent(project_pgid, 1.0):
                raise AuditRecoveryError(
                    "POSIX project group survived recovery termination",
                    execution_id=resource.resource_id,
                    backend="posix",
                    operation="recover",
                )
        await self._cleanup_owned_cgroup(resource)
        return RecoveryDisposition.TERMINATED

    async def _wait_process_absent(self, pid: int, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while process_exists(pid):
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(self._poll)
        return True

    async def _wait_group_absent(self, pgid: int, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while process_group_exists(pgid):
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(self._poll)
        return True

    async def _cleanup_owned_cgroup(
        self,
        resource: BackendResourceIdentifier,
    ) -> None:
        details = resource_native_details(resource)
        raw_path = details.get("cgroup_path")
        if raw_path is None:
            return
        path = Path(raw_path)
        expected_digest = hashlib.sha256(
            f"{resource.resource_id}\0{resource.ownership_token}".encode()
        ).hexdigest()[:24]
        if (
            not path.is_absolute()
            or path.name != f"truecoder-{expected_digest}"
            or Path("/sys/fs/cgroup") not in path.parents
        ):
            raise AuditRecoveryError(
                "persisted cgroup identity is not execution-owned",
                execution_id=resource.resource_id,
                backend="posix",
                operation="recover_cgroup",
            )
        if not path.exists():
            return
        kill_path = path / "cgroup.kill"
        if kill_path.exists():
            kill_path.write_text("1", encoding="ascii")
        for _attempt in range(40):
            try:
                path.rmdir()
                return
            except FileNotFoundError:
                return
            except OSError:
                await asyncio.sleep(self._poll)
        raise AuditRecoveryError(
            "owned cgroup survived recovery cleanup",
            execution_id=resource.resource_id,
            backend="posix",
            operation="recover_cgroup",
        )


def _positive_int(details: dict[str, str], name: str) -> int:
    try:
        value = int(details[name])
    except (KeyError, ValueError) as exc:
        raise AuditRecoveryError(
            f"POSIX recovery identity is missing {name}",
            backend="posix",
            operation="recover",
        ) from exc
    if value <= 0:
        raise AuditRecoveryError(
            f"POSIX recovery identity has invalid {name}",
            backend="posix",
            operation="recover",
        )
    return value
