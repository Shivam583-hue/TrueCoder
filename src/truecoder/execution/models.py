from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

ExecutionStatus = Literal[
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "denied",
    "limit_exceeded",
    "failed_to_start",
]

ExecutionLifecycleStage = Literal[
    "requested",
    "policy_evaluated",
    "backend_selected",
    "starting",
    "started",
    "terminating",
    "completed",
]

ExecutionMode = Literal["exec", "shell"]

BackendPreference = Literal["auto", "local", "container"]
BackendName = Literal["posix", "windows", "container"]

ShellKind = Literal["auto", "posix", "powershell"]
ResolvedShellKind = Literal["posix", "powershell"]

FilesystemMode = Literal[
    "host",
    "workspace-read",
    "workspace-write",
]

WORKSPACE_FILESYSTEM_MODES: frozenset[str] = frozenset(
    {"workspace-read", "workspace-write"}
)

CapabilityLevel = Literal[
    "unsupported",
    "best_effort",
    "enforced",
]

TerminationReason = Literal[
    "timeout",
    "cancellation",
    "output_limit",
    "shutdown",
]

_EXECUTION_MODES: frozenset[str] = frozenset({"exec", "shell"})


# ---------------------- Validation helpers -------------------------


def _require_finite(value: float | int, name: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def _require_positive(value: float | int, name: str) -> None:
    _require_finite(value, name)

    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")


def _require_nonnegative(value: float | int, name: str) -> None:
    _require_finite(value, name)

    if value < 0:
        raise ValueError(f"{name} cannot be negative")


def _require_no_null_bytes(value: str, name: str) -> None:
    if "\x00" in value:
        raise ValueError(f"{name} cannot contain null bytes")


def _require_utc(value: datetime, name: str) -> None:
    offset = value.utcoffset()

    if offset is None:
        raise ValueError(f"{name} must be timezone-aware")

    if offset != timedelta(0):
        raise ValueError(f"{name} must be expressed in UTC")


def _is_absolute_path(path: Path) -> bool:
    """Absolute under *either* path flavour.

    ``Path("/workspace").is_absolute()`` is False on Windows because the path
    carries no drive letter. A Windows host is perfectly entitled to build a
    request that targets a POSIX container, so absoluteness is checked against
    both flavours rather than against the host's.
    """
    text = str(path)

    return PurePosixPath(text).is_absolute() or PureWindowsPath(text).is_absolute()


# ----------------------- Request model -----------------------


@dataclass(frozen=True)
class ExecutionLimits:
    timeout_seconds: float
    max_output_bytes: int
    max_return_bytes: int
    memory_bytes: int | None = None
    cpu_seconds: float | None = None
    max_processes: int | None = None
    termination_grace_seconds: float = 2.0

    def __post_init__(self) -> None:
        _require_positive(self.timeout_seconds, "timeout_seconds")
        _require_positive(self.max_output_bytes, "max_output_bytes")

        _require_nonnegative(self.max_return_bytes, "max_return_bytes")
        _require_nonnegative(
            self.termination_grace_seconds, "termination_grace_seconds"
        )

        if self.max_return_bytes > self.max_output_bytes:
            raise ValueError("max_return_bytes cannot exceed max_output_bytes")

        optional_positive = {
            "memory_bytes": self.memory_bytes,
            "cpu_seconds": self.cpu_seconds,
            "max_processes": self.max_processes,
        }

        for name, value in optional_positive.items():
            if value is not None:
                _require_positive(value, name)


@dataclass(frozen=True)
class ExecutionRequest:
    """A request as authored by the caller, before a backend is chosen.

    Deliberately platform-agnostic. ``working_directory`` and ``environment``
    are validated but *not* normalized, because normalization depends on the
    target platform, which is not known until the ``backend_selected``
    lifecycle stage. See ``normalize_environment_for_backend`` and
    ``resolve_shell_kind``.
    """

    mode: ExecutionMode
    argv: tuple[str, ...] | None
    script: str | None
    working_directory: Path
    limits: ExecutionLimits
    network_access: bool
    filesystem_mode: FilesystemMode
    backend: BackendPreference = "auto"
    shell_kind: ShellKind = "auto"
    environment: tuple[tuple[str, str], ...] = ()
    require_cancellation: bool = True

    def __post_init__(self) -> None:
        if self.mode not in _EXECUTION_MODES:
            raise ValueError(f"unknown execution mode: {self.mode!r}")

        if self.mode == "exec":
            if not self.argv or self.script is not None:
                raise ValueError("exec mode requires argv and forbids script")

            for index, argument in enumerate(self.argv):
                _require_no_null_bytes(argument, f"argv[{index}]")

            if not self.argv[0]:
                raise ValueError("argv[0] cannot be empty")

        else:
            if self.script is None or self.argv is not None:
                raise ValueError("shell mode requires script and forbids argv")

            _require_no_null_bytes(self.script, "script")

        self._validate_working_directory()
        self._validate_environment(self.environment)

    def _validate_working_directory(self) -> None:
        text = str(self.working_directory)

        _require_no_null_bytes(text, "working_directory")

        if text.startswith("~"):
            raise ValueError("working_directory must not contain a '~' prefix")

        if not _is_absolute_path(self.working_directory):
            raise ValueError("working_directory must be an absolute path")

    @staticmethod
    def _validate_environment(environment: tuple[tuple[str, str], ...]) -> None:
        seen: set[str] = set()

        for key, value in environment:
            _require_no_null_bytes(key, "environment variable name")
            _require_no_null_bytes(value, f"value of environment variable {key!r}")

            if not key:
                raise ValueError("environment variable names cannot be empty")

            if any(character.isspace() for character in key):
                raise ValueError(
                    f"environment variable name cannot contain whitespace: {key!r}"
                )

            if "=" in key:
                raise ValueError("environment variable names cannot contain '='")

            if key in seen:
                raise ValueError(f"duplicate environment variable: {key!r}")

            seen.add(key)


def normalize_environment_for_backend(
    environment: tuple[tuple[str, str], ...],
    backend: BackendName,
) -> tuple[tuple[str, str], ...]:
    """Fold environment variable names for the *target* platform.

    Windows environment blocks are case-insensitive; POSIX ones are not. The
    request cannot make this call itself, because a Windows host may target a
    POSIX container and vice versa.
    """
    if backend != "windows":
        return environment

    seen: dict[str, str] = {}
    normalized: list[tuple[str, str]] = []

    for key, value in environment:
        folded = key.upper()

        if folded in seen:
            raise ValueError(
                "environment variables collide case-insensitively on Windows: "
                f"{seen[folded]!r} and {key!r}"
            )

        seen[folded] = key
        normalized.append((folded, value))

    return tuple(normalized)


def resolve_shell_kind(
    shell_kind: ShellKind, backend: BackendName
) -> ResolvedShellKind:
    """Turn the request's ``ShellKind`` into the ``ResolvedShellKind`` that
    ``BackendCapabilities.supported_shells`` is expressed in.

    An explicit choice is passed through unchanged; whether the backend can
    honour it is a capability question, not a resolution one.
    """
    if shell_kind != "auto":
        return shell_kind

    return "powershell" if backend == "windows" else "posix"


def validate_workspace_containment(
    request: ExecutionRequest,
    context: ExecutionContext,
    backend: BackendName,
) -> None:
    """Reject workspace-scoped executions that start outside the project root.

    Only meaningful for local backends: a container's working directory lives
    in the container's namespace and has no relationship to the host's
    ``project_root``.
    """
    if backend == "container":
        return

    if request.filesystem_mode not in WORKSPACE_FILESYSTEM_MODES:
        return

    resolved = request.working_directory.expanduser().resolve(strict=False)

    if not resolved.is_relative_to(context.project_root):
        raise ValueError(
            "working_directory must be inside project_root for filesystem mode "
            f"{request.filesystem_mode!r}"
        )


# --------------------------------------------------------


@dataclass(frozen=True)
class BackendCapabilities:
    filesystem_isolation: CapabilityLevel
    network_isolation: CapabilityLevel
    memory_limits: CapabilityLevel
    cpu_limits: CapabilityLevel
    process_limits: CapabilityLevel
    timeout_enforcement: CapabilityLevel
    cancellation: CapabilityLevel

    supported_execution_modes: tuple[ExecutionMode, ...]
    supported_filesystem_modes: tuple[FilesystemMode, ...]
    supported_shells: tuple[ResolvedShellKind, ...]

    def __post_init__(self) -> None:
        required = {
            "supported_execution_modes": self.supported_execution_modes,
            "supported_filesystem_modes": self.supported_filesystem_modes,
            "supported_shells": self.supported_shells,
        }

        for name, values in required.items():
            if not values:
                raise ValueError(f"{name} cannot be empty")

            if len(values) != len(set(values)):
                raise ValueError(f"{name} cannot contain duplicates")


@dataclass(frozen=True)
class CapabilityCheck:
    compatible: bool
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.compatible and not self.reasons:
            raise ValueError("an incompatible CapabilityCheck must give reasons")


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    termination_reason: TerminationReason | None
    backend: BackendName
    audit_id: str

    def __post_init__(self) -> None:
        if not self.audit_id:
            raise ValueError("audit_id cannot be empty")

        _require_nonnegative(self.duration_seconds, "duration_seconds")
        _require_nonnegative(self.stdout_bytes, "stdout_bytes")
        _require_nonnegative(self.stderr_bytes, "stderr_bytes")

        if self.status == "completed":
            if self.exit_code is None:
                raise ValueError("a completed execution must report an exit code")

            if self.termination_reason is not None:
                raise ValueError(
                    "a completed execution cannot carry a termination reason"
                )

        # These two never reached the point of having a process to exit.
        if self.status in {"denied", "failed_to_start"} and self.exit_code is not None:
            raise ValueError(f"{self.status} executions cannot report an exit code")


@dataclass(frozen=True)
class ExecutionContext:
    execution_id: str
    tool_call_id: str
    session_id: str | None
    turn_id: str | None
    project_root: Path
    launched_at_utc: datetime

    def __post_init__(self) -> None:
        if not self.execution_id:
            raise ValueError("execution_id cannot be empty")

        if not self.tool_call_id:
            raise ValueError("tool_call_id cannot be empty")

        _require_utc(self.launched_at_utc, "launched_at_utc")

        expanded = self.project_root.expanduser()

        if not expanded.is_absolute():
            raise ValueError("project_root must be an absolute path")

        object.__setattr__(
            self,
            "project_root",
            expanded.resolve(strict=False),
        )


@dataclass(frozen=True)
class NativeDiagnostic:
    code: int | str | None
    message: str
    platform: str


@dataclass(frozen=True)
class ExecutionLifecycleEvent:
    execution_id: str
    stage: ExecutionLifecycleStage
    occurred_at_utc: datetime
    sequence: int
    message: str | None = None
    details: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.execution_id:
            raise ValueError("execution_id cannot be empty")

        if self.sequence < 0:
            raise ValueError("sequence cannot be negative")

        _require_utc(self.occurred_at_utc, "occurred_at_utc")

        keys = [key for key, _ in self.details]

        if any(not key for key in keys):
            raise ValueError("lifecycle detail keys cannot be empty")

        if len(keys) != len(set(keys)):
            raise ValueError("lifecycle detail keys must be unique")
