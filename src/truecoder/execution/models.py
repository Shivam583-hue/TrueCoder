# models.py answers:
#
# What information exists?
#
# backends/base.py answers:
#
# What must every backend implement?
#
# discovery.py answers:
#
# Which backend can satisfy this request?
#
# service.py answers:
#
# In what order does execution happen?
#
# posix.py, windows.py, and container.py answer:
#
# How is it enforced on this platform?

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal, TypeAlias

ExecutionStatus: TypeAlias = Literal[
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "denied",
    "limit_exceeded",
    "failed_to_start",
]

ExecutionLifecycleStage: TypeAlias = Literal[
    "requested",
    "policy_evaluated",
    "approval_required",
    "approved",
    "backend_selected",
    "starting",
    "started",
    "terminating",
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "denied",
    "limit_exceeded",
    "failed_to_start",
]

ExecutionMode: TypeAlias = Literal["exec", "shell"]
BackendPreference: TypeAlias = Literal["auto", "local", "container"]
BackendName: TypeAlias = Literal["posix", "windows", "container"]
ExecutionPlatform: TypeAlias = Literal["posix", "windows"]

ShellKind: TypeAlias = Literal["auto", "posix", "powershell"]
ResolvedShellKind: TypeAlias = Literal["posix", "powershell"]

FilesystemMode: TypeAlias = Literal[
    "host",
    "workspace-read",
    "workspace-write",
]

CapabilityLevel: TypeAlias = Literal[
    "unsupported",
    "best_effort",
    "enforced",
]

TerminationReason: TypeAlias = Literal[
    "timeout",
    "cancellation",
    "output_limit",
    "memory_limit",
    "cpu_limit",
    "process_limit",
    "shutdown",
]


EXECUTION_STATUSES: Final = frozenset(
    {
        "completed",
        "failed",
        "timed_out",
        "cancelled",
        "denied",
        "limit_exceeded",
        "failed_to_start",
    }
)

EXECUTION_LIFECYCLE_STAGES: Final = frozenset(
    {
        "requested",
        "policy_evaluated",
        "approval_required",
        "approved",
        "backend_selected",
        "starting",
        "started",
        "terminating",
        "completed",
        "failed",
        "timed_out",
        "cancelled",
        "denied",
        "limit_exceeded",
        "failed_to_start",
    }
)

EXECUTION_MODES: Final = frozenset({"exec", "shell"})
BACKEND_PREFERENCES: Final = frozenset({"auto", "local", "container"})
BACKEND_NAMES: Final = frozenset({"posix", "windows", "container"})
EXECUTION_PLATFORMS: Final = frozenset({"posix", "windows"})
SHELL_KINDS: Final = frozenset({"auto", "posix", "powershell"})
RESOLVED_SHELL_KINDS: Final = frozenset({"posix", "powershell"})

FILESYSTEM_MODES: Final = frozenset({"host", "workspace-read", "workspace-write"})
WORKSPACE_FILESYSTEM_MODES: Final = frozenset({"workspace-read", "workspace-write"})

CAPABILITY_LEVELS: Final = frozenset({"unsupported", "best_effort", "enforced"})

TERMINATION_REASONS: Final = frozenset(
    {
        "timeout",
        "cancellation",
        "output_limit",
        "memory_limit",
        "cpu_limit",
        "process_limit",
        "shutdown",
    }
)

LIMIT_TERMINATION_REASONS: Final = frozenset(
    {"output_limit", "memory_limit", "cpu_limit", "process_limit"}
)


# ---------------------- Validation helpers -------------------------


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _require_nonempty_string(value: object, name: str) -> str:
    text = _require_string(value, name)
    if not text.strip():
        raise ValueError(f"{name} cannot be empty or whitespace-only")
    return text


def _require_no_null_bytes(value: str, name: str) -> None:
    if "\x00" in value:
        raise ValueError(f"{name} cannot contain null bytes")


def _require_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")


def _require_choice(
    value: object,
    allowed: frozenset[str],
    name: str,
) -> str:
    text = _require_string(value, name)
    if text not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"unknown {name}: {text!r}; expected one of: {choices}")
    return text


def _require_number(value: object, name: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")

    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be finite")

    return value


def _require_positive_number(value: object, name: str) -> None:
    if _require_number(value, name) <= 0:
        raise ValueError(f"{name} must be greater than zero")


def _require_nonnegative_number(value: object, name: str) -> None:
    if _require_number(value, name) < 0:
        raise ValueError(f"{name} cannot be negative")


def _require_positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")


def _require_nonnegative_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} cannot be negative")


def _require_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")

    offset = value.utcoffset()
    if offset is None:
        raise ValueError(f"{name} must be timezone-aware")
    if offset != timedelta(0):
        raise ValueError(f"{name} must be expressed in UTC")

    return value


def _normalize_absolute_host_path(value: object, name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{name} must be a pathlib.Path")

    _require_no_null_bytes(str(value), name)
    expanded = value.expanduser()

    if not expanded.is_absolute():
        raise ValueError(f"{name} must be an absolute host path")

    return expanded.resolve(strict=False)


def normalize_environment_name(
    name: str,
    platform: ExecutionPlatform,
) -> str:

    name = _require_nonempty_string(name, "environment variable name")
    _require_no_null_bytes(name, "environment variable name")
    _require_choice(platform, EXECUTION_PLATFORMS, "execution platform")

    if platform == "windows":
        return name.casefold()
    return name


def _validate_choice_tuple(
    values: object,
    *,
    name: str,
    allowed: frozenset[str],
    allow_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")

    if not allow_empty and not values:
        raise ValueError(f"{name} cannot be empty")

    if len(values) != len(set(values)):
        raise ValueError(f"{name} cannot contain duplicates")

    for value in values:
        _require_choice(value, allowed, name)

    return values


def _validate_string_pairs(
    values: object,
    *,
    name: str,
    require_nonempty_keys: bool = True,
    forbid_equals_in_keys: bool = False,
    forbid_null_bytes: bool = False,
    key_platform: ExecutionPlatform | None = None,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple of key/value pairs")

    seen: set[str] = set()

    for index, item in enumerate(values):
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError(f"{name}[{index}] must be a two-item tuple")

        key, value = item
        key = _require_string(key, f"{name}[{index}].key")
        value = _require_string(value, f"{name}[{index}].value")

        if require_nonempty_keys and not key.strip():
            raise ValueError(f"{name} keys cannot be empty")

        if forbid_equals_in_keys and "=" in key:
            raise ValueError(f"{name} keys cannot contain '='")

        if forbid_null_bytes:
            _require_no_null_bytes(key, f"{name}[{index}].key")
            _require_no_null_bytes(value, f"{name}[{index}].value")

        comparison_key = (
            normalize_environment_name(key, key_platform)
            if key_platform is not None
            else key
        )

        if comparison_key in seen:
            raise ValueError(f"duplicate {name} key: {key!r}")

        seen.add(comparison_key)

    return values


# ----------------------- Request model -----------------------


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    timeout_seconds: float
    max_output_bytes: int
    max_return_bytes: int
    memory_bytes: int | None = None
    cpu_seconds: float | None = None
    max_processes: int | None = None
    termination_grace_seconds: float = 2.0

    def __post_init__(self) -> None:
        _require_positive_number(self.timeout_seconds, "timeout_seconds")
        _require_positive_int(self.max_output_bytes, "max_output_bytes")
        _require_nonnegative_int(self.max_return_bytes, "max_return_bytes")
        _require_nonnegative_number(
            self.termination_grace_seconds,
            "termination_grace_seconds",
        )

        if self.max_return_bytes > self.max_output_bytes:
            raise ValueError("max_return_bytes cannot exceed max_output_bytes")

        if self.memory_bytes is not None:
            _require_positive_int(self.memory_bytes, "memory_bytes")

        if self.cpu_seconds is not None:
            _require_positive_number(self.cpu_seconds, "cpu_seconds")

        if self.max_processes is not None:
            _require_positive_int(self.max_processes, "max_processes")


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
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
        _require_choice(self.mode, EXECUTION_MODES, "execution mode")
        _require_choice(self.backend, BACKEND_PREFERENCES, "backend preference")
        _require_choice(self.shell_kind, SHELL_KINDS, "shell kind")
        _require_choice(self.filesystem_mode, FILESYSTEM_MODES, "filesystem mode")
        _require_bool(self.network_access, "network_access")
        _require_bool(self.require_cancellation, "require_cancellation")

        if not isinstance(self.limits, ExecutionLimits):
            raise TypeError("limits must be an ExecutionLimits instance")

        if self.mode == "exec":
            self._validate_exec_mode()
        else:
            self._validate_shell_mode()

        object.__setattr__(
            self,
            "working_directory",
            _normalize_absolute_host_path(
                self.working_directory,
                "working_directory",
            ),
        )

        _validate_string_pairs(
            self.environment,
            name="environment",
            require_nonempty_keys=True,
            forbid_equals_in_keys=True,
            forbid_null_bytes=True,
            key_platform=(
                "windows"
                if self.shell_kind == "powershell"
                else "posix"
                if self.shell_kind == "posix"
                else None
            ),
        )

    def _validate_exec_mode(self) -> None:
        if self.script is not None:
            raise ValueError("exec mode forbids script")

        if self.shell_kind != "auto":
            raise ValueError("exec mode requires shell_kind='auto'")

        if not isinstance(self.argv, tuple) or not self.argv:
            raise ValueError("exec mode requires a non-empty argv tuple")

        for index, argument in enumerate(self.argv):
            argument = _require_string(argument, f"argv[{index}]")
            _require_no_null_bytes(argument, f"argv[{index}]")

        _require_nonempty_string(self.argv[0], "argv[0]")

    def _validate_shell_mode(self) -> None:
        if self.argv is not None:
            raise ValueError("shell mode forbids argv")

        if self.script is None:
            raise ValueError("shell mode requires script")

        script = _require_nonempty_string(self.script, "script")
        _require_no_null_bytes(script, "script")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reason: str | None
    effective_limits: ExecutionLimits

    def __post_init__(self) -> None:
        _require_bool(self.allowed, "allowed")

        if not isinstance(self.effective_limits, ExecutionLimits):
            raise TypeError("effective_limits must be an ExecutionLimits instance")

        if self.reason is not None:
            _require_nonempty_string(self.reason, "reason")

        if not self.allowed and self.reason is None:
            raise ValueError("a denied policy decision must include a reason")


# ---------------------Backend capability models--------------------


@dataclass(frozen=True, slots=True)
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
        for name, value in (
            ("filesystem_isolation", self.filesystem_isolation),
            ("network_isolation", self.network_isolation),
            ("memory_limits", self.memory_limits),
            ("cpu_limits", self.cpu_limits),
            ("process_limits", self.process_limits),
            ("timeout_enforcement", self.timeout_enforcement),
            ("cancellation", self.cancellation),
        ):
            _require_choice(value, CAPABILITY_LEVELS, name)

        _validate_choice_tuple(
            self.supported_execution_modes,
            name="supported_execution_modes",
            allowed=EXECUTION_MODES,
            allow_empty=False,
        )
        _validate_choice_tuple(
            self.supported_filesystem_modes,
            name="supported_filesystem_modes",
            allowed=FILESYSTEM_MODES,
            allow_empty=False,
        )
        _validate_choice_tuple(
            self.supported_shells,
            name="supported_shells",
            allowed=RESOLVED_SHELL_KINDS,
            allow_empty=True,
        )

        supports_shell = "shell" in self.supported_execution_modes

        if supports_shell and not self.supported_shells:
            raise ValueError("a shell-capable backend must declare at least one shell")

        if not supports_shell and self.supported_shells:
            raise ValueError("an exec-only backend cannot declare shells")


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    compatible: bool
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_bool(self.compatible, "compatible")

        if not isinstance(self.reasons, tuple):
            raise TypeError("reasons must be a tuple")

        for index, reason in enumerate(self.reasons):
            _require_nonempty_string(reason, f"reasons[{index}]")

        if len(self.reasons) != len(set(self.reasons)):
            raise ValueError("reasons cannot contain duplicates")

        if self.compatible and self.reasons:
            raise ValueError("a compatible CapabilityCheck cannot contain reasons")

        if not self.compatible and not self.reasons:
            raise ValueError("an incompatible CapabilityCheck must contain reasons")


# ---------------------- Result, context, and audit models ----------------------


@dataclass(frozen=True, slots=True)
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
    backend: BackendName | None
    audit_id: str

    def __post_init__(self) -> None:
        _require_choice(self.status, EXECUTION_STATUSES, "execution status")
        _require_string(self.stdout, "stdout")
        _require_string(self.stderr, "stderr")
        _require_nonnegative_number(self.duration_seconds, "duration_seconds")
        _require_nonnegative_int(self.stdout_bytes, "stdout_bytes")
        _require_nonnegative_int(self.stderr_bytes, "stderr_bytes")
        _require_bool(self.stdout_truncated, "stdout_truncated")
        _require_bool(self.stderr_truncated, "stderr_truncated")
        _require_nonempty_string(self.audit_id, "audit_id")

        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise TypeError("exit_code must be an integer or None")

        if self.backend is not None:
            _require_choice(self.backend, BACKEND_NAMES, "backend name")

        if self.termination_reason is not None:
            _require_choice(
                self.termination_reason,
                TERMINATION_REASONS,
                "termination reason",
            )

        self._validate_status_invariants()

    def _validate_status_invariants(self) -> None:
        if self.status == "completed":
            if self.exit_code != 0:
                raise ValueError("completed execution requires exit_code 0")
            if self.backend is None:
                raise ValueError("completed execution requires a backend")
            if self.termination_reason is not None:
                raise ValueError("completed execution cannot have a termination reason")
            return

        if self.status == "denied":
            if self.exit_code is not None:
                raise ValueError("denied execution cannot have an exit code")
            if self.backend is not None:
                raise ValueError("denied execution cannot have a backend")
            if self.termination_reason is not None:
                raise ValueError("denied execution cannot have a termination reason")
            return

        if self.status == "failed_to_start":
            if self.exit_code is not None:
                raise ValueError("failed-to-start execution cannot have an exit code")
            if self.termination_reason is not None:
                raise ValueError(
                    "failed-to-start execution cannot have a termination reason"
                )
            return

        if self.backend is None:
            raise ValueError(f"{self.status} execution requires a backend")

        if self.status == "timed_out":
            if self.termination_reason != "timeout":
                raise ValueError(
                    "timed-out execution requires termination_reason='timeout'"
                )
            return

        if self.status == "cancelled":
            if self.termination_reason not in {"cancellation", "shutdown"}:
                raise ValueError(
                    "cancelled execution requires cancellation or shutdown "
                    "as its termination reason"
                )
            return

        if self.status == "limit_exceeded":
            if self.termination_reason not in LIMIT_TERMINATION_REASONS:
                raise ValueError(
                    "limit-exceeded execution must identify the exceeded limit"
                )
            return

        if self.status == "failed":
            if self.exit_code is None or self.exit_code == 0:
                raise ValueError("failed execution requires a nonzero exit code")
            if self.termination_reason is not None:
                raise ValueError(
                    "failed execution cannot have a termination reason; use "
                    "timed_out, cancelled, or limit_exceeded instead"
                )


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    execution_id: str
    tool_call_id: str
    session_id: str
    turn_id: str
    workspace_id: str
    project_root: Path
    launched_at_utc: datetime

    def __post_init__(self) -> None:
        _require_nonempty_string(self.execution_id, "execution_id")
        _require_nonempty_string(self.tool_call_id, "tool_call_id")
        _require_nonempty_string(self.session_id, "session_id")
        _require_nonempty_string(self.turn_id, "turn_id")
        _require_nonempty_string(self.workspace_id, "workspace_id")

        _require_utc(self.launched_at_utc, "launched_at_utc")

        object.__setattr__(
            self,
            "project_root",
            _normalize_absolute_host_path(self.project_root, "project_root"),
        )


@dataclass(frozen=True, slots=True)
class NativeDiagnostic:
    code: int | str | None
    message: str
    platform: str

    def __post_init__(self) -> None:
        if isinstance(self.code, bool) or not isinstance(
            self.code,
            (int, str, type(None)),
        ):
            raise TypeError("code must be an integer, string, or None")

        if isinstance(self.code, str):
            _require_nonempty_string(self.code, "code")

        _require_nonempty_string(self.message, "message")
        _require_nonempty_string(self.platform, "platform")


@dataclass(frozen=True, slots=True)
class ExecutionLifecycleEvent:
    execution_id: str
    stage: ExecutionLifecycleStage
    occurred_at_utc: datetime
    sequence: int
    message: str | None = None
    details: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty_string(self.execution_id, "execution_id")
        _require_choice(
            self.stage,
            EXECUTION_LIFECYCLE_STAGES,
            "execution lifecycle stage",
        )
        _require_utc(self.occurred_at_utc, "occurred_at_utc")
        _require_nonnegative_int(self.sequence, "sequence")

        if self.message is not None:
            _require_nonempty_string(self.message, "message")

        _validate_string_pairs(
            self.details,
            name="lifecycle details",
            require_nonempty_keys=True,
        )
