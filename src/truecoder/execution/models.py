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

ExecutionMode = Literal["exec", "shell"]
BackendPreference = Literal["auto", "local", "sandbox"]
ShellKind = Literal["auto", "posix", "powershell"]
FilesystemMode = Literal["workspace-read", "workspace-write"]


@dataclass(frozen=True)
class ExecutionLimits:
    timeout_seconds: float
    max_output_bytes: int
    max_return_bytes: int
    memory_bytes: int | None
    cpu_seconds: float | None
    max_processes: int | None
    termination_grace_seconds: float


@dataclass(frozen=True)
class ExecutionRequest:
    mode: ExecutionMode
    argv: tuple[str, ...] | None
    script: str | None
    working_directory: Path
    limits: ExecutionLimits
    network_access: bool
    filesystem_mode: FilesystemMode
    backend: BackendPreference
    shell_kind: ShellKind
    environment: tuple[tuple[str, str], ...] = ()


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
    backend: str
    audit_id: str


@dataclass(frozen=True)
class ExecutionContext:
    execution_id: str
    tool_call_id: str
    session_id: str | None
    turn_id: str | None
    project_root: Path
    launched_at_utc: datetime
