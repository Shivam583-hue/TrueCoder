from __future__ import annotations

import hashlib
import os
import platform
import socket
from dataclasses import dataclass
from pathlib import Path

from ..audit.models import BackendResourceIdentifier
from ..models import ExecutionContext
from .posix_plan import POSIX_PROTOCOL_VERSION

_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))


@dataclass(frozen=True, slots=True)
class PosixProcessFacts:
    pid: int
    process_group_id: int
    session_id: int
    start_ticks: int | None
    state: str


@dataclass(frozen=True, slots=True)
class PosixIdentityVerification:
    matches: bool
    resource_absent: bool
    reason: str

    def __post_init__(self) -> None:
        if self.matches and self.resource_absent:
            raise ValueError("a matching resource cannot be absent")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("verification reason must not be empty")


def create_posix_resource(
    context: ExecutionContext,
    *,
    supervisor_pid: int,
    project_pgid: int,
    ownership_token: str,
    cgroup_path: Path | None,
) -> BackendResourceIdentifier:
    if not isinstance(context, ExecutionContext):
        raise TypeError("context must be ExecutionContext")
    _require_pid(supervisor_pid, "supervisor_pid")
    _require_pid(project_pgid, "project_pgid")
    if not isinstance(ownership_token, str) or not ownership_token:
        raise ValueError("ownership_token must not be empty")
    facts = read_process_facts(supervisor_pid)
    if facts is None:
        raise ProcessLookupError("supervisor disappeared before identity creation")
    if facts.session_id != supervisor_pid:
        raise ValueError("supervisor must be its POSIX session leader")

    details: list[tuple[str, str]] = [
        ("supervisor_pid", str(supervisor_pid)),
        ("project_pgid", str(project_pgid)),
        ("protocol_version", str(POSIX_PROTOCOL_VERSION)),
    ]
    boot_id = current_boot_id()
    if boot_id is not None:
        details.append(("boot_id", boot_id))
    if facts.start_ticks is not None:
        details.append(("supervisor_start_ticks", str(facts.start_ticks)))
    if cgroup_path is not None:
        if not cgroup_path.is_absolute():
            raise ValueError("cgroup_path must be absolute")
        details.append(("cgroup_path", str(cgroup_path.resolve(strict=False))))
    return BackendResourceIdentifier(
        version=1,
        backend="posix",
        resource_kind="supervised-process-group",
        resource_id=context.execution_id,
        ownership_token=ownership_token,
        host_id=current_host_id(),
        created_at_utc=context.launched_at_utc,
        native_details=tuple(details),
    )


def verify_posix_resource(
    resource: BackendResourceIdentifier,
) -> PosixIdentityVerification:
    if not isinstance(resource, BackendResourceIdentifier):
        raise TypeError("resource must be BackendResourceIdentifier")
    if (
        resource.backend != "posix"
        or resource.resource_kind != "supervised-process-group"
    ):
        return PosixIdentityVerification(False, False, "resource-kind-mismatch")
    if resource.host_id != current_host_id():
        return PosixIdentityVerification(False, False, "host-mismatch")
    details = resource_native_details(resource)
    if details.get("protocol_version") != str(POSIX_PROTOCOL_VERSION):
        return PosixIdentityVerification(False, False, "protocol-mismatch")
    try:
        supervisor_pid = int(details["supervisor_pid"])
        project_pgid = int(details["project_pgid"])
    except (KeyError, ValueError):
        return PosixIdentityVerification(False, False, "invalid-process-identity")

    facts = read_process_facts(supervisor_pid)
    if facts is None:
        return PosixIdentityVerification(False, True, "supervisor-absent")
    if facts.session_id != supervisor_pid:
        return PosixIdentityVerification(False, False, "session-mismatch")
    if platform.system().casefold() == "linux":
        boot_id = current_boot_id()
        if boot_id is None or details.get("boot_id") != boot_id:
            return PosixIdentityVerification(False, False, "boot-mismatch")
        try:
            expected_ticks = int(details["supervisor_start_ticks"])
        except (KeyError, ValueError):
            return PosixIdentityVerification(False, False, "start-time-missing")
        if facts.start_ticks != expected_ticks:
            return PosixIdentityVerification(False, False, "start-time-mismatch")
        if not process_group_exists(project_pgid):
            return PosixIdentityVerification(False, False, "project-group-absent")
        return PosixIdentityVerification(True, False, "exact-match")
    return PosixIdentityVerification(False, False, "ownership-unverifiable")


def resource_native_details(
    resource: BackendResourceIdentifier,
) -> dict[str, str]:
    return dict(resource.native_details)


def current_host_id() -> str:
    parts = [platform.system(), platform.machine(), socket.gethostname()]
    for path in _MACHINE_ID_PATHS:
        try:
            machine_id = path.read_text(encoding="ascii").strip()
        except OSError:
            continue
        if machine_id:
            parts.append(machine_id)
            break
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def current_boot_id() -> str | None:
    if platform.system().casefold() != "linux":
        return None
    try:
        value = _BOOT_ID_PATH.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value or None


def read_process_facts(pid: int) -> PosixProcessFacts | None:
    _require_pid(pid, "pid")
    if platform.system().casefold() == "linux":
        return _read_linux_process_facts(pid)
    try:
        process_group_id = os.getpgid(pid)
        session_id = os.getsid(pid)
    except ProcessLookupError:
        return None
    return PosixProcessFacts(
        pid=pid,
        process_group_id=process_group_id,
        session_id=session_id,
        start_ticks=None,
        state="unknown",
    )


def process_group_exists(process_group_id: int) -> bool:
    _require_pid(process_group_id, "process_group_id")
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_exists(pid: int) -> bool:
    facts = read_process_facts(pid)
    return facts is not None and facts.state != "Z"


def _read_linux_process_facts(pid: int) -> PosixProcessFacts | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    closing = text.rfind(")")
    if closing < 0:
        return None
    fields = text[closing + 2 :].split()
    if len(fields) <= 19:
        return None
    try:
        return PosixProcessFacts(
            pid=pid,
            state=fields[0],
            process_group_id=int(fields[2]),
            session_id=int(fields[3]),
            start_ticks=int(fields[19]),
        )
    except ValueError:
        return None


def _require_pid(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value
