from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ..environment import ConstructedEnvironment
from ..models import (
    ExecutionLimits,
    ExecutionRequest,
    ResolvedShellKind,
)
from .models import DiscoveredProgram, SelectedBackend

POSIX_PROTOCOL_VERSION: Final = 1
MAX_POSIX_PLAN_ENVIRONMENT_ENTRIES: Final = 256


@dataclass(frozen=True, slots=True)
class PosixLaunchPlan:
    protocol_version: int
    execution_id: str
    argv: tuple[str, ...]
    working_directory: Path
    environment: tuple[tuple[str, str], ...] = field(repr=False)
    limits: ExecutionLimits
    shell_kind: ResolvedShellKind | None
    cgroup_path: Path | None = None

    def __post_init__(self) -> None:
        if self.protocol_version != POSIX_PROTOCOL_VERSION:
            raise ValueError("unsupported POSIX launch-plan version")
        if not isinstance(self.execution_id, str) or not self.execution_id.strip():
            raise ValueError("execution_id must not be empty")
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ValueError("argv must be a non-empty tuple")
        for argument in self.argv:
            if not isinstance(argument, str):
                raise TypeError("argv must contain strings")
            if "\x00" in argument:
                raise ValueError("argv must not contain null bytes")
        if not self.argv[0]:
            raise ValueError("argv[0] must not be empty")
        if not isinstance(self.working_directory, Path):
            raise TypeError("working_directory must be a pathlib.Path")
        if not self.working_directory.is_absolute():
            raise ValueError("working_directory must be absolute")
        object.__setattr__(
            self,
            "working_directory",
            self.working_directory.resolve(strict=False),
        )
        if not isinstance(self.environment, tuple):
            raise TypeError("environment must be a tuple")
        if len(self.environment) > MAX_POSIX_PLAN_ENVIRONMENT_ENTRIES:
            raise ValueError("environment contains too many entries")
        names: set[str] = set()
        for item in self.environment:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("environment entries must be two-item tuples")
            name, value = item
            if not isinstance(name, str) or not isinstance(value, str):
                raise TypeError("environment names and values must be strings")
            if not name or "=" in name or "\x00" in name or "\x00" in value:
                raise ValueError("environment contains an invalid entry")
            if name in names:
                raise ValueError("environment names must be unique")
            names.add(name)
        if not isinstance(self.limits, ExecutionLimits):
            raise TypeError("limits must be ExecutionLimits")
        if self.shell_kind not in {None, "posix", "powershell"}:
            raise ValueError("unknown resolved shell kind")
        if self.cgroup_path is not None:
            if not isinstance(self.cgroup_path, Path):
                raise TypeError("cgroup_path must be a pathlib.Path or None")
            if not self.cgroup_path.is_absolute():
                raise ValueError("cgroup_path must be absolute")
            object.__setattr__(
                self,
                "cgroup_path",
                self.cgroup_path.resolve(strict=False),
            )


def build_posix_launch_plan(
    request: ExecutionRequest,
    selected: SelectedBackend,
    environment: ConstructedEnvironment,
    shells: tuple[DiscoveredProgram, ...],
    *,
    execution_id: str,
    cgroup_path: Path | None = None,
) -> PosixLaunchPlan:
    if not isinstance(request, ExecutionRequest):
        raise TypeError("request must be ExecutionRequest")
    if not isinstance(selected, SelectedBackend):
        raise TypeError("selected must be SelectedBackend")
    if selected.descriptor.name != "posix":
        raise ValueError("selected backend must be posix")
    if request.filesystem_mode != "host":
        raise ValueError("the POSIX local backend supports host filesystem mode only")
    if not isinstance(environment, ConstructedEnvironment):
        raise TypeError("environment must be ConstructedEnvironment")
    if not environment.valid:
        raise ValueError("environment contains policy violations")
    if not isinstance(shells, tuple):
        raise TypeError("shells must be a tuple")

    shell_kind = selected.resolved_shell
    if request.mode == "exec":
        if shell_kind is not None:
            raise ValueError("exec mode cannot have a resolved shell")
        assert request.argv is not None
        argv = request.argv
    else:
        if shell_kind is None:
            raise ValueError("shell mode requires a resolved shell")
        shell = _resolve_shell_program(shells, shell_kind)
        assert request.script is not None
        argv = (
            (str(shell.path), "-lc", request.script)
            if shell_kind == "posix"
            else (
                str(shell.path),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                request.script,
            )
        )

    return PosixLaunchPlan(
        protocol_version=POSIX_PROTOCOL_VERSION,
        execution_id=execution_id,
        argv=argv,
        working_directory=request.working_directory,
        environment=environment.variables,
        limits=request.limits,
        shell_kind=shell_kind,
        cgroup_path=cgroup_path,
    )


def plan_to_payload(plan: PosixLaunchPlan) -> dict[str, object]:
    if not isinstance(plan, PosixLaunchPlan):
        raise TypeError("plan must be PosixLaunchPlan")
    limits = plan.limits
    return {
        "protocol_version": plan.protocol_version,
        "execution_id": plan.execution_id,
        "argv": list(plan.argv),
        "working_directory": str(plan.working_directory),
        "environment": [list(item) for item in plan.environment],
        "limits": {
            "timeout_seconds": limits.timeout_seconds,
            "max_output_bytes": limits.max_output_bytes,
            "max_return_bytes": limits.max_return_bytes,
            "memory_bytes": limits.memory_bytes,
            "cpu_seconds": limits.cpu_seconds,
            "max_processes": limits.max_processes,
            "termination_grace_seconds": limits.termination_grace_seconds,
        },
        "shell_kind": plan.shell_kind,
        "cgroup_path": (
            str(plan.cgroup_path) if plan.cgroup_path is not None else None
        ),
    }


def plan_from_payload(payload: object) -> PosixLaunchPlan:
    data = _require_object(
        payload,
        {
            "protocol_version",
            "execution_id",
            "argv",
            "working_directory",
            "environment",
            "limits",
            "shell_kind",
            "cgroup_path",
        },
        "plan",
    )
    raw_argv = data["argv"]
    if not isinstance(raw_argv, list):
        raise TypeError("plan.argv must be a list")
    raw_environment = data["environment"]
    if not isinstance(raw_environment, list):
        raise TypeError("plan.environment must be a list")
    environment: list[tuple[str, str]] = []
    for item in raw_environment:
        if not isinstance(item, list) or len(item) != 2:
            raise TypeError("plan.environment entries must be two-item lists")
        name, value = item
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("plan.environment entries must contain strings")
        environment.append((name, value))
    limits_data = _require_object(
        data["limits"],
        {
            "timeout_seconds",
            "max_output_bytes",
            "max_return_bytes",
            "memory_bytes",
            "cpu_seconds",
            "max_processes",
            "termination_grace_seconds",
        },
        "plan.limits",
    )
    shell_kind = data["shell_kind"]
    cgroup_path = data["cgroup_path"]
    if shell_kind not in {None, "posix", "powershell"}:
        raise ValueError("plan.shell_kind is invalid")
    if cgroup_path is not None and not isinstance(cgroup_path, str):
        raise TypeError("plan.cgroup_path must be a string or null")
    return PosixLaunchPlan(
        protocol_version=_require_int(
            data["protocol_version"],
            "plan.protocol_version",
        ),
        execution_id=_require_string(data["execution_id"], "plan.execution_id"),
        argv=tuple(
            _require_string(argument, "plan.argv item") for argument in raw_argv
        ),
        working_directory=Path(
            _require_string(
                data["working_directory"],
                "plan.working_directory",
            )
        ),
        environment=tuple(environment),
        limits=ExecutionLimits(
            timeout_seconds=_require_number(
                limits_data["timeout_seconds"],
                "plan.limits.timeout_seconds",
            ),
            max_output_bytes=_require_int(
                limits_data["max_output_bytes"],
                "plan.limits.max_output_bytes",
            ),
            max_return_bytes=_require_int(
                limits_data["max_return_bytes"],
                "plan.limits.max_return_bytes",
            ),
            memory_bytes=_require_optional_int(
                limits_data["memory_bytes"],
                "plan.limits.memory_bytes",
            ),
            cpu_seconds=_require_optional_number(
                limits_data["cpu_seconds"],
                "plan.limits.cpu_seconds",
            ),
            max_processes=_require_optional_int(
                limits_data["max_processes"],
                "plan.limits.max_processes",
            ),
            termination_grace_seconds=_require_number(
                limits_data["termination_grace_seconds"],
                "plan.limits.termination_grace_seconds",
            ),
        ),
        shell_kind=shell_kind,
        cgroup_path=Path(cgroup_path) if cgroup_path is not None else None,
    )


def _resolve_shell_program(
    shells: tuple[DiscoveredProgram, ...],
    kind: ResolvedShellKind,
) -> DiscoveredProgram:
    for shell in shells:
        if not isinstance(shell, DiscoveredProgram):
            raise TypeError("shells must contain DiscoveredProgram values")
        if shell.shell_kind == kind:
            return shell
    raise ValueError(f"no discovered {kind} shell is available")


def _require_object(
    value: object,
    keys: set[str],
    name: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    if set(value) != keys:
        raise ValueError(f"{name} fields are invalid")
    return value


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _require_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    return float(value)


def _require_optional_int(value: object, name: str) -> int | None:
    return None if value is None else _require_int(value, name)


def _require_optional_number(value: object, name: str) -> float | None:
    return None if value is None else _require_number(value, name)
