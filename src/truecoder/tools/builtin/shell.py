from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypedDict

from pydantic import Field, model_validator

from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.defaults import DEFAULT_EXECUTION_LIMITS
from truecoder.execution.errors import ExecutionInfrastructureError
from truecoder.execution.models import (
    ExecutionContext,
    ExecutionLimits,
    ExecutionRequest,
    ExecutionResult,
)
from truecoder.tools.base import (
    BaseTool,
    ToolApproval,
    ToolArguments,
    ToolExecutionError,
)
from truecoder.tools.context import ToolInvocationContext

DEFAULT_SHELL_LIMITS = DEFAULT_EXECUTION_LIMITS


@dataclass(frozen=True, slots=True)
class ShellDefaults:
    limits: ExecutionLimits = DEFAULT_SHELL_LIMITS

    def __post_init__(self) -> None:
        if not isinstance(self.limits, ExecutionLimits):
            raise TypeError("limits must be an ExecutionLimits")


class ShellArguments(ToolArguments):
    mode: Literal["exec", "shell"] = Field(
        default="exec",
        description=(
            "Use exec with argv for ordinary commands. Use shell only when "
            "pipes, redirects, chaining, substitutions, or other shell syntax "
            "is required."
        ),
    )
    argv: tuple[str, ...] | None = Field(
        default=None,
        description="Exact executable and argument boundaries for exec mode.",
    )
    script: str | None = Field(
        default=None,
        description="Shell source used only in shell mode.",
    )
    working_directory: str = Field(
        default=".",
        min_length=1,
        description="Existing workspace-relative directory.",
    )
    backend: Literal["auto", "local", "container"] = "auto"
    filesystem_mode: Literal[
        "host",
        "workspace-read",
        "workspace-write",
    ] = "workspace-read"
    network_access: bool = False
    shell_kind: Literal["auto", "posix", "powershell"] = "auto"
    timeout_seconds: float | None = Field(default=None, gt=0)
    max_output_bytes: int | None = Field(default=None, gt=0)
    max_return_bytes: int | None = Field(default=None, ge=0)
    memory_bytes: int | None = Field(default=None, gt=0)
    cpu_seconds: float | None = Field(default=None, gt=0)
    max_processes: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_mode_contract(self) -> ShellArguments:
        if self.mode == "exec":
            if not self.argv or not self.argv[0].strip():
                raise ValueError("exec mode requires a non-empty argv")
            if self.script is not None:
                raise ValueError("exec mode forbids script")
            if self.shell_kind != "auto":
                raise ValueError("exec mode requires shell_kind='auto'")
        else:
            if self.argv is not None:
                raise ValueError("shell mode forbids argv")
            if self.script is None or not self.script.strip():
                raise ValueError("shell mode requires a non-empty script")
        if (
            self.max_output_bytes is not None
            and self.max_return_bytes is not None
            and self.max_return_bytes > self.max_output_bytes
        ):
            raise ValueError("max_return_bytes cannot exceed max_output_bytes")
        return self


class ShellOutput(TypedDict):
    status: str
    exit_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    termination_reason: str | None
    backend: str | None
    audit_id: str
    reason_code: str | None
    reason_message: str | None


class ShellExecutionService(Protocol):
    async def execute(
        self,
        request: ExecutionRequest,
        context: ExecutionContext,
        *,
        cancellation_source: CancellationSource,
    ) -> ExecutionResult: ...


class ShellTool(BaseTool[ShellArguments]):
    name = "shell"
    description = (
        "Run a bounded command through TrueCoder's execution service. Prefer "
        "mode='exec' with argv for ordinary commands; use mode='shell' only "
        "when shell syntax is required."
    )
    arguments_type = ShellArguments
    approval = ToolApproval.NOT_REQUIRED

    def __init__(
        self,
        project_root: Path,
        service: ShellExecutionService,
        defaults: ShellDefaults | None = None,
    ) -> None:
        if not isinstance(project_root, Path):
            raise TypeError("project_root must be a pathlib.Path")
        try:
            resolved_root = project_root.resolve(strict=True)
        except OSError as error:
            raise ValueError("project_root must exist and be accessible") from error
        if not resolved_root.is_dir():
            raise ValueError("project_root must be a directory")
        if not callable(getattr(service, "execute", None)):
            raise TypeError("service must provide an async execute method")
        if defaults is not None and not isinstance(defaults, ShellDefaults):
            raise TypeError("defaults must be ShellDefaults")
        self._project_root = resolved_root
        self._service = service
        self._defaults = defaults or ShellDefaults()

    async def run(
        self,
        arguments: ShellArguments,
        invocation: ToolInvocationContext | None = None,
    ) -> ShellOutput:
        if invocation is None:
            raise ToolExecutionError(
                "Shell execution requires an invocation context.",
                code="missing_invocation_context",
            )
        request = build_shell_request(
            arguments,
            project_root=self._project_root,
            defaults=self._defaults,
        )
        try:
            result = await self._service.execute(
                request,
                invocation.execution,
                cancellation_source=invocation.cancellation_source,
            )
        except ExecutionInfrastructureError as error:
            raise ToolExecutionError(
                "Shell execution infrastructure could not complete safely.",
                code="shell_infrastructure_error",
            ) from error
        return format_shell_result(result)


def build_shell_request(
    arguments: ShellArguments,
    *,
    project_root: Path,
    defaults: ShellDefaults,
) -> ExecutionRequest:
    if not isinstance(arguments, ShellArguments):
        raise TypeError("arguments must be ShellArguments")
    if not isinstance(project_root, Path):
        raise TypeError("project_root must be a pathlib.Path")
    if not isinstance(defaults, ShellDefaults):
        raise TypeError("defaults must be ShellDefaults")

    working_directory = _working_directory(
        project_root,
        arguments.working_directory,
    )
    return ExecutionRequest(
        mode=arguments.mode,
        argv=arguments.argv,
        script=arguments.script,
        working_directory=working_directory,
        limits=_effective_limits(arguments, defaults.limits),
        network_access=arguments.network_access,
        filesystem_mode=arguments.filesystem_mode,
        backend=arguments.backend,
        shell_kind=arguments.shell_kind,
    )


def format_shell_result(result: ExecutionResult) -> ShellOutput:
    if not isinstance(result, ExecutionResult):
        raise TypeError("result must be an ExecutionResult")
    return {
        "status": result.status,
        "exit_code": result.exit_code,
        "duration_seconds": round(result.duration_seconds, 3),
        "stdout": result.stdout,
        "stderr": result.stderr,
        "stdout_bytes": result.stdout_bytes,
        "stderr_bytes": result.stderr_bytes,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
        "termination_reason": result.termination_reason,
        "backend": result.backend,
        "audit_id": result.audit_id,
        "reason_code": result.reason_code,
        "reason_message": result.reason_message,
    }


def _working_directory(project_root: Path, requested: str) -> Path:
    try:
        root = project_root.resolve(strict=True)
    except OSError as error:
        raise ToolExecutionError(
            "The project root is unavailable.",
            code="workspace_unavailable",
        ) from error
    relative = Path(requested)
    if relative.is_absolute():
        raise ToolExecutionError(
            "The working directory must be workspace-relative.",
            code="outside_workspace",
        )
    candidate = (root / relative).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise ToolExecutionError(
            "The working directory is outside the workspace.",
            code="outside_workspace",
        )
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise ToolExecutionError(
            "The working directory does not exist.",
            code="directory_not_found",
        ) from error
    except OSError as error:
        raise ToolExecutionError(
            "The working directory could not be resolved.",
            code="invalid_working_directory",
        ) from error
    if not resolved.is_relative_to(root):
        raise ToolExecutionError(
            "The working directory is outside the workspace.",
            code="outside_workspace",
        )
    if not resolved.is_dir():
        raise ToolExecutionError(
            "The working directory is not a directory.",
            code="not_a_directory",
        )
    return resolved


def _effective_limits(
    arguments: ShellArguments,
    defaults: ExecutionLimits,
) -> ExecutionLimits:
    timeout_seconds = _stricter(
        arguments.timeout_seconds,
        defaults.timeout_seconds,
    )
    max_output_bytes = int(
        _stricter(arguments.max_output_bytes, defaults.max_output_bytes)
    )
    max_return_bytes = min(
        int(_stricter(arguments.max_return_bytes, defaults.max_return_bytes)),
        max_output_bytes,
    )
    return ExecutionLimits(
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        max_return_bytes=max_return_bytes,
        memory_bytes=_stricter_optional(
            arguments.memory_bytes,
            defaults.memory_bytes,
        ),
        cpu_seconds=_stricter_optional(
            arguments.cpu_seconds,
            defaults.cpu_seconds,
        ),
        max_processes=_stricter_optional(
            arguments.max_processes,
            defaults.max_processes,
        ),
        termination_grace_seconds=defaults.termination_grace_seconds,
    )


def _stricter(requested: float | None, default: float):
    return default if requested is None else min(requested, default)


def _stricter_optional(
    requested: float | None,
    default: float | None,
):
    if requested is None:
        return default
    if default is None:
        return requested
    return min(requested, default)
