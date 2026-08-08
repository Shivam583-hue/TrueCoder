from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..errors import BackendStartError
from ..models import ExecutionLimits
from .models import CgroupV2Info


class CgroupIO(Protocol):
    def make_directory(self, path: Path) -> None: ...

    def write_text(self, path: Path, value: str) -> None: ...

    def read_text(self, path: Path) -> str: ...

    def exists(self, path: Path) -> bool: ...

    def remove_directory(self, path: Path) -> None: ...


class SystemCgroupIO:
    def make_directory(self, path: Path) -> None:
        path.mkdir(mode=0o700)

    def write_text(self, path: Path, value: str) -> None:
        path.write_text(value, encoding="ascii")

    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="ascii")

    def exists(self, path: Path) -> bool:
        return path.exists()

    def remove_directory(self, path: Path) -> None:
        path.rmdir()


@dataclass(frozen=True, slots=True)
class CgroupCounters:
    cpu_usage_usec: int
    oom_kills: int
    pids_max_events: int


@dataclass(frozen=True, slots=True)
class PosixCgroup:
    path: Path
    delegated_root: Path
    controllers: tuple[str, ...]
    baseline: CgroupCounters

    def __post_init__(self) -> None:
        for name, value in (
            ("path", self.path),
            ("delegated_root", self.delegated_root),
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(f"{name} must be an absolute path")
        try:
            self.path.relative_to(self.delegated_root)
        except ValueError as exc:
            raise ValueError(
                "cgroup path must remain beneath its delegated root"
            ) from exc
        if not isinstance(self.controllers, tuple):
            raise TypeError("controllers must be a tuple")


def create_execution_cgroup(
    info: CgroupV2Info | None,
    *,
    execution_id: str,
    ownership_token: str,
    limits: ExecutionLimits,
    io: CgroupIO | None = None,
) -> PosixCgroup | None:
    if info is None or not info.mounted or not info.writable:
        return None
    if info.delegated_path is None:
        raise BackendStartError(
            "cgroup discovery did not provide a delegated path",
            execution_id=execution_id,
            backend="posix",
            operation="create_cgroup",
        )
    required = _required_controllers(limits)
    enabled = frozenset(info.enabled_controllers)
    enforced = tuple(sorted(required & enabled))
    if not enforced:
        return None

    adapter = io or SystemCgroupIO()
    root = info.delegated_path.resolve(strict=False)
    digest = hashlib.sha256(f"{execution_id}\0{ownership_token}".encode()).hexdigest()[
        :24
    ]
    path = (root / f"truecoder-{digest}").resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise BackendStartError(
            "derived cgroup path escaped its delegated root",
            execution_id=execution_id,
            backend="posix",
            operation="create_cgroup",
        ) from exc

    try:
        adapter.make_directory(path)
        if limits.memory_bytes is not None and "memory" in enforced:
            adapter.write_text(path / "memory.max", str(limits.memory_bytes))
            if adapter.exists(path / "memory.oom.group"):
                adapter.write_text(path / "memory.oom.group", "1")
        if limits.max_processes is not None and "pids" in enforced:
            adapter.write_text(path / "pids.max", str(limits.max_processes))
        baseline = read_cgroup_counters(path, io=adapter)
    except OSError as exc:
        try:
            adapter.remove_directory(path)
        except OSError:
            pass
        raise BackendStartError(
            "failed to configure the execution cgroup",
            execution_id=execution_id,
            backend="posix",
            operation="create_cgroup",
        ) from exc
    return PosixCgroup(
        path=path,
        delegated_root=root,
        controllers=enforced,
        baseline=baseline,
    )


def attach_current_process(
    path: Path,
    *,
    io: CgroupIO | None = None,
) -> None:
    (io or SystemCgroupIO()).write_text(path / "cgroup.procs", str(os.getpid()))


def read_cgroup_counters(
    path: Path,
    *,
    io: CgroupIO | None = None,
) -> CgroupCounters:
    adapter = io or SystemCgroupIO()
    return CgroupCounters(
        cpu_usage_usec=_read_key(adapter, path / "cpu.stat", "usage_usec"),
        oom_kills=_read_key(adapter, path / "memory.events", "oom_kill"),
        pids_max_events=_read_key(adapter, path / "pids.events", "max"),
    )


def kill_cgroup(
    cgroup: PosixCgroup,
    *,
    io: CgroupIO | None = None,
) -> None:
    adapter = io or SystemCgroupIO()
    kill_path = cgroup.path / "cgroup.kill"
    if adapter.exists(kill_path):
        adapter.write_text(kill_path, "1")


def cleanup_cgroup(
    cgroup: PosixCgroup,
    *,
    io: CgroupIO | None = None,
) -> None:
    adapter = io or SystemCgroupIO()
    kill_cgroup(cgroup, io=adapter)
    adapter.remove_directory(cgroup.path)


def limit_reason(
    cgroup: PosixCgroup,
    *,
    cpu_limit_seconds: float | None,
    io: CgroupIO | None = None,
) -> str | None:
    current = read_cgroup_counters(cgroup.path, io=io)
    if current.oom_kills > cgroup.baseline.oom_kills:
        return "memory_limit"
    if current.pids_max_events > cgroup.baseline.pids_max_events:
        return "process_limit"
    if (
        cpu_limit_seconds is not None
        and "cpu" in cgroup.controllers
        and current.cpu_usage_usec - cgroup.baseline.cpu_usage_usec
        >= int(cpu_limit_seconds * 1_000_000)
    ):
        return "cpu_limit"
    return None


def _required_controllers(limits: ExecutionLimits) -> frozenset[str]:
    return frozenset(
        controller
        for controller, value in (
            ("memory", limits.memory_bytes),
            ("cpu", limits.cpu_seconds),
            ("pids", limits.max_processes),
        )
        if value is not None
    )


def _read_key(io: CgroupIO, path: Path, key: str) -> int:
    if not io.exists(path):
        return 0
    for line in io.read_text(path).splitlines():
        name, separator, value = line.partition(" ")
        if name == key and separator:
            try:
                return max(0, int(value.strip()))
            except ValueError:
                return 0
    return 0
