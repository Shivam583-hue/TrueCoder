from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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
        nonnegative = {
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_return_bytes": self.max_return_bytes,
            "termination_grace_seconds": self.termination_grace_seconds,
        }

        for name, value in nonnegative.items():
            if value < 0:
                raise ValueError(f"{name} cannot be negative")

        optional_nonnegative = {
            "memory_bytes": self.memory_bytes,
            "cpu_seconds": self.cpu_seconds,
            "max_processes": self.max_processes,
        }

        for name, value in optional_nonnegative.items():
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True)
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
        if self.mode == "exec":
            if not self.argv or self.script is not None:
                raise ValueError("exec mode requires argv and forbids script")

        elif self.mode == "shell":
            if self.script is None or self.argv is not None:
                raise ValueError("shell mode requires script and forbids argv")

        normalized_directory = self.working_directory.expanduser().resolve(strict=False)
        object.__setattr__(
            self,
            "working_directory",
            normalized_directory,
        )

        object.__setattr__(
            self,
            "environment",
            self._normalize_environment(self.environment),
        )

    @staticmethod
    def _normalize_environment(
        environment: tuple[tuple[str, str], ...],
    ) -> tuple[tuple[str, str], ...]:
        is_windows = sys.platform == "win32"

        seen: set[str] = set()
        normalized: list[tuple[str, str]] = []

        for key, value in environment:
            if "\x00" in key or "\x00" in value:
                raise ValueError("environment variables cannot contain null bytes")

            if not key or not key.strip():
                raise ValueError("environment variable names cannot be empty")

            if "=" in key:
                raise ValueError("environment variable names cannot contain '='")

            normalized_key = key.upper() if is_windows else key
            duplicate_key = normalized_key if is_windows else key

            if duplicate_key in seen:
                raise ValueError(f"duplicate environment variable: {key!r}")

            seen.add(duplicate_key)
            normalized.append((normalized_key, value))

        return tuple(normalized)


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


@dataclass(frozen=True)
class CapabilityCheck:
    compatible: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    stdout_bytes: int
    stderr_bytes: int
    output_truncated: bool
    termination_reason: str | None
    backend: BackendName
    audit_id: str


@dataclass(frozen=True)
class ExecutionContext:
    execution_id: str
    tool_call_id: str
    session_id: str | None
    turn_id: str | None
    project_root: Path
    launched_at_utc: datetime

    def __post_init__(self) -> None:
        canonical_root = self.project_root.expanduser().resolve(strict=False)

        if not canonical_root.is_absolute():
            raise ValueError("project_root must be an absolute path")

        if self.launched_at_utc.tzinfo is None:
            raise ValueError("launched_at_utc must be timezone-aware")

        object.__setattr__(
            self,
            "project_root",
            canonical_root,
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

        if self.occurred_at_utc.tzinfo is None:
            raise ValueError("occurred_at_utc must be timezone-aware")

        keys = [key for key, _ in self.details]

        if any(not key for key in keys):
            raise ValueError("lifecycle detail keys cannot be empty")

        if len(keys) != len(set(keys)):
            raise ValueError("lifecycle detail keys must be unique")
