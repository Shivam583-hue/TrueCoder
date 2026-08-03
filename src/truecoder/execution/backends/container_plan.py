from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from ..errors import BackendStartError
from ..models import ExecutionLimits, ExecutionRequest, ResolvedShellKind
from ..preparation import PreparedExecution
from .base import BackendStartContext
from .container_models import (
    CONTAINER_WORKSPACE,
    PLAN_VERSION,
    ContainerCreatePlan,
    ContainerImage,
    ContainerLabels,
    ContainerMount,
    ContainerSecurityProfile,
    ContainerTmpfs,
    NetworkMode,
)
from .models import BackendDescriptor

NAME_PREFIX: Final = "truecoder-exec"
DEFAULT_MEMORY_BYTES: Final = 512 * 1024 * 1024
DEFAULT_PIDS_LIMIT: Final = 64
DEFAULT_TMP_BYTES: Final = 64 * 1024 * 1024
DEFAULT_RUN_BYTES: Final = 8 * 1024 * 1024
DEFAULT_HOME_BYTES: Final = 16 * 1024 * 1024
CONTAINER_SHELL: Final = "/bin/sh"
CPU_BUDGET_VARIABLE: Final = "TRUECODER_CPU_SECONDS"


@dataclass(frozen=True, slots=True)
class ContainerLaunchConfig:
    image: ContainerImage
    default_memory_bytes: int = DEFAULT_MEMORY_BYTES
    default_pids_limit: int = DEFAULT_PIDS_LIMIT
    cpu_rate_ceiling: float | None = None
    isolated_network: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.image, ContainerImage):
            raise TypeError("image must be a ContainerImage")
        if self.isolated_network is not None:
            if not isinstance(self.isolated_network, str):
                raise TypeError("isolated_network must be a string or None")
            if not self.isolated_network.strip():
                raise ValueError("isolated_network cannot be empty")


def build_container_plan(
    prepared: PreparedExecution,
    context: BackendStartContext,
    descriptor: BackendDescriptor,
    config: ContainerLaunchConfig,
    *,
    ownership_token: str,
    env_file: Path | None = None,
) -> ContainerCreatePlan:
    if not isinstance(prepared, PreparedExecution):
        raise TypeError("prepared must be a PreparedExecution")
    if not isinstance(context, BackendStartContext):
        raise TypeError("context must be a BackendStartContext")
    if not isinstance(config, ContainerLaunchConfig):
        raise TypeError("config must be a ContainerLaunchConfig")

    _require_exact_descriptor(prepared, descriptor, context)
    request = prepared.request
    execution = context.execution

    read_only = request.filesystem_mode != "workspace-write"
    workspace = _canonical_directory(execution.project_root, "project root", context)
    _require_sandbox_access(workspace, config.image, context, writable=not read_only)
    workdir = _container_workdir(workspace, request.working_directory, context)

    if request.filesystem_mode == "host":
        raise BackendStartError(
            "the container sandbox never exposes the host filesystem mode",
            execution_id=execution.execution_id,
            backend="container",
            operation="plan",
        )

    limits = request.limits
    security = ContainerSecurityProfile(
        memory_bytes=_memory_bytes(limits, config),
        pids_limit=_pids_limit(limits, config),
        cpu_rate=config.cpu_rate_ceiling,
        network_mode=_network_mode(request, config, context),
        tmpfs=_tmpfs(config.image),
    )

    return ContainerCreatePlan(
        runtime="docker",
        name=f"{NAME_PREFIX}-{ownership_token[:32]}",
        image=config.image,
        labels=ContainerLabels(
            execution_id=execution.execution_id,
            audit_run_id=context.audit_run_id,
            ownership_token=ownership_token,
            image_digest=config.image.digest,
        ),
        mounts=(
            ContainerMount(
                source=workspace,
                target=CONTAINER_WORKSPACE,
                read_only=read_only,
            ),
        ),
        security=security,
        workdir=workdir,
        argv=_argv(request, prepared.resolved_shell, context),
        env_file=env_file,
        plan_version=PLAN_VERSION,
    )


def build_env_file_content(
    prepared: PreparedExecution,
    *,
    cpu_seconds: float | None = None,
) -> str:
    lines: list[str] = []
    for name, value in prepared.environment.variables:
        if "\n" in value or "\r" in value:
            raise ValueError(f"environment value for {name} contains a newline")
        lines.append(f"{name}={value}")
    if cpu_seconds is not None and cpu_seconds > 0:
        lines.append(f"{CPU_BUDGET_VARIABLE}={cpu_seconds}")
    return "\n".join(lines) + ("\n" if lines else "")


def load_image_lock(path: Path) -> ContainerImage:
    if not isinstance(path, Path):
        raise TypeError("path must be a pathlib.Path")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("the image lock must contain a JSON object")

    required = ("reference", "digest", "platform", "user")
    missing = tuple(name for name in required if name not in payload)
    if missing:
        raise ValueError(f"the image lock is missing: {', '.join(missing)}")

    return ContainerImage(
        reference=str(payload["reference"]),
        digest=str(payload["digest"]),
        platform=payload["platform"],
        user=str(payload["user"]),
        entrypoint_version=(
            str(payload["entrypoint_version"])
            if payload.get("entrypoint_version") is not None
            else None
        ),
    )


def _require_exact_descriptor(
    prepared: PreparedExecution,
    descriptor: BackendDescriptor,
    context: BackendStartContext,
) -> None:
    if prepared.backend.name != "container":
        raise BackendStartError(
            "this plan requires a container backend selection",
            execution_id=context.execution_id,
            backend="container",
            operation="plan",
        )
    if prepared.backend != descriptor:
        raise BackendStartError(
            "the container descriptor changed after preparation",
            execution_id=context.execution_id,
            backend="container",
            operation="plan",
        )


def _canonical_directory(
    value: Path,
    name: str,
    context: BackendStartContext,
) -> Path:
    try:
        resolved = value.resolve(strict=True)
    except OSError as error:
        raise BackendStartError(
            f"the {name} could not be resolved",
            execution_id=context.execution_id,
            backend="container",
            operation="plan",
        ) from error
    if not resolved.is_dir():
        raise BackendStartError(
            f"the {name} is not a directory",
            execution_id=context.execution_id,
            backend="container",
            operation="plan",
        )
    return resolved


def _require_sandbox_access(
    workspace: Path,
    image: ContainerImage,
    context: BackendStartContext,
    *,
    writable: bool,
) -> None:
    info = workspace.stat()
    owned = info.st_uid == image.uid
    grouped = info.st_gid == image.gid

    readable = bool(info.st_mode & 0o005) or (owned and info.st_mode & 0o500)
    if grouped and info.st_mode & 0o050:
        readable = True
    if not readable:
        raise BackendStartError(
            "the workspace is not readable by the non-root sandbox user "
            f"{image.user}; the mount would be unusable",
            execution_id=context.execution_id,
            backend="container",
            operation="plan",
        )

    if not writable:
        return

    allowed = bool(info.st_mode & 0o002) or (owned and info.st_mode & 0o200)
    if grouped and info.st_mode & 0o020:
        allowed = True
    if not allowed:
        raise BackendStartError(
            "workspace-write requires a workspace the sandbox user "
            f"{image.user} can write; grant access or use workspace-read",
            execution_id=context.execution_id,
            backend="container",
            operation="plan",
        )


def _container_workdir(
    workspace: Path,
    working_directory: Path,
    context: BackendStartContext,
) -> PurePosixPath:
    resolved = _canonical_directory(working_directory, "working directory", context)
    try:
        relative = resolved.relative_to(workspace)
    except ValueError as error:
        raise BackendStartError(
            "the working directory escapes the workspace mount",
            execution_id=context.execution_id,
            backend="container",
            operation="plan",
        ) from error
    if not relative.parts:
        return CONTAINER_WORKSPACE
    return CONTAINER_WORKSPACE.joinpath(*relative.parts)


def _memory_bytes(limits: ExecutionLimits, config: ContainerLaunchConfig) -> int:
    requested = limits.memory_bytes
    if requested is None:
        return config.default_memory_bytes
    return min(int(requested), config.default_memory_bytes)


def _pids_limit(limits: ExecutionLimits, config: ContainerLaunchConfig) -> int:
    requested = limits.max_processes
    if requested is None:
        return config.default_pids_limit
    return min(int(requested), config.default_pids_limit)


def _network_mode(
    request: ExecutionRequest,
    config: ContainerLaunchConfig,
    context: BackendStartContext,
) -> NetworkMode:
    if not request.network_access:
        return "none"
    if config.isolated_network is None:
        raise BackendStartError(
            "network access requires a configured isolated runtime network",
            execution_id=context.execution_id,
            backend="container",
            operation="plan",
        )
    return "isolated"


def _tmpfs(image: ContainerImage) -> tuple[ContainerTmpfs, ...]:
    return (
        ContainerTmpfs(
            target=PurePosixPath("/tmp"),
            size_bytes=DEFAULT_TMP_BYTES,
            uid=image.uid,
            gid=image.gid,
        ),
        ContainerTmpfs(
            target=PurePosixPath("/run"),
            size_bytes=DEFAULT_RUN_BYTES,
            uid=image.uid,
            gid=image.gid,
        ),
        ContainerTmpfs(
            target=PurePosixPath("/home/truecoder"),
            size_bytes=DEFAULT_HOME_BYTES,
            uid=image.uid,
            gid=image.gid,
        ),
    )


def _argv(
    request: ExecutionRequest,
    resolved_shell: ResolvedShellKind | None,
    context: BackendStartContext,
) -> tuple[str, ...]:
    if request.mode == "exec":
        if not request.argv:
            raise BackendStartError(
                "an exec request requires argv",
                execution_id=context.execution_id,
                backend="container",
                operation="plan",
            )
        return tuple(request.argv)

    if resolved_shell != "posix":
        raise BackendStartError(
            "the container sandbox only provides the pinned POSIX shell",
            execution_id=context.execution_id,
            backend="container",
            operation="plan",
        )
    if not request.script:
        raise BackendStartError(
            "a shell request requires a script",
            execution_id=context.execution_id,
            backend="container",
            operation="plan",
        )
    return (CONTAINER_SHELL, "-c", request.script)
