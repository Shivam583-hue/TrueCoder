from __future__ import annotations

from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Final

from ..models import ExecutionLimits, ResolvedShellKind
from ..preparation import PreparedExecution
from .models import DiscoveredProgram

STILL_ACTIVE: Final = 259
CREATE_SUSPENDED_SENTINEL: Final = 0x00000004
JOB_OBJECT_TERMINATION_EXIT_CODE: Final = 0xC000013A

_ERROR_FILE_NOT_FOUND: Final = 2
_ERROR_PATH_NOT_FOUND: Final = 3
_ERROR_ACCESS_DENIED: Final = 5
_ERROR_INVALID_HANDLE: Final = 6
_ERROR_NOT_ENOUGH_MEMORY: Final = 8
_ERROR_BAD_EXE_FORMAT: Final = 193
_ERROR_DIRECTORY: Final = 267

_START_ERROR_CODES: Final[dict[int, str]] = {
    _ERROR_FILE_NOT_FOUND: "executable-not-found",
    _ERROR_PATH_NOT_FOUND: "working-directory-not-found",
    _ERROR_ACCESS_DENIED: "permission-denied",
    _ERROR_BAD_EXE_FORMAT: "not-an-executable",
    _ERROR_DIRECTORY: "working-directory-invalid",
    _ERROR_NOT_ENOUGH_MEMORY: "insufficient-memory",
    _ERROR_INVALID_HANDLE: "invalid-handle",
}

_NTSTATUS_TERMINATION: Final[dict[int, str]] = {
    0xC000013A: "control-c-exit",
    0xC0000005: "access-violation",
    0xC00000FD: "stack-overflow",
    0xC0000409: "stack-buffer-overrun",
    0xC0000374: "heap-corruption",
    0x40010004: "debugger-terminated",
}


def normalize_start_error(error_code: int) -> str:
    if isinstance(error_code, bool) or not isinstance(error_code, int):
        raise TypeError("error_code must be an integer")
    return _START_ERROR_CODES.get(error_code, "process-creation-failed")


def normalize_exit_code(raw_exit_code: int) -> tuple[int, str | None]:
    if isinstance(raw_exit_code, bool) or not isinstance(raw_exit_code, int):
        raise TypeError("raw_exit_code must be an integer")
    unsigned = raw_exit_code & 0xFFFFFFFF
    detail = _NTSTATUS_TERMINATION.get(unsigned)
    if unsigned >= 0x80000000:
        return unsigned - 0x100000000, detail
    return unsigned, detail


def quote_argument(argument: str) -> str:
    if not isinstance(argument, str):
        raise TypeError("argument must be a string")
    if argument and not any(character in argument for character in ' \t\n\v"'):
        return argument

    quoted = ['"']
    backslashes = 0
    for character in argument:
        if character == "\\":
            backslashes += 1
            continue
        if character == '"':
            quoted.append("\\" * (backslashes * 2 + 1))
            quoted.append('"')
        else:
            quoted.append("\\" * backslashes)
            quoted.append(character)
        backslashes = 0
    quoted.append("\\" * (backslashes * 2))
    quoted.append('"')
    return "".join(quoted)


def build_command_line(argv: tuple[str, ...]) -> str:
    if not isinstance(argv, tuple):
        raise TypeError("argv must be a tuple")
    if not argv:
        raise ValueError("argv cannot be empty")
    for entry in argv:
        if not isinstance(entry, str):
            raise TypeError("argv entries must be strings")
    return " ".join(quote_argument(entry) for entry in argv)


def quote_powershell_literal(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    return "'" + value.replace("'", "''") + "'"


def build_powershell_argv(shell_path: str, script: str) -> tuple[str, ...]:
    if not isinstance(shell_path, str) or not shell_path.strip():
        raise ValueError("shell_path cannot be empty")
    if not isinstance(script, str) or not script.strip():
        raise ValueError("script cannot be empty")
    return (
        shell_path,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    )


def build_command_shell_argv(shell_path: str, script: str) -> tuple[str, ...]:
    if not isinstance(shell_path, str) or not shell_path.strip():
        raise ValueError("shell_path cannot be empty")
    if not isinstance(script, str) or not script.strip():
        raise ValueError("script cannot be empty")
    return (shell_path, "/d", "/s", "/c", script)


@dataclass(frozen=True, slots=True)
class WindowsJobLimits:
    memory_bytes: int | None
    cpu_seconds: float | None
    max_processes: int | None
    kill_on_job_close: bool = True

    def __post_init__(self) -> None:
        for name in ("memory_bytes", "max_processes"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer or None")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.cpu_seconds is not None:
            if isinstance(self.cpu_seconds, bool) or not isinstance(
                self.cpu_seconds,
                (int, float),
            ):
                raise TypeError("cpu_seconds must be a number or None")
            if self.cpu_seconds <= 0:
                raise ValueError("cpu_seconds must be positive")

    @property
    def cpu_100ns_ticks(self) -> int | None:
        if self.cpu_seconds is None:
            return None
        return int(self.cpu_seconds * 10_000_000)


@dataclass(frozen=True, slots=True)
class WindowsLaunchPlan:
    argv: tuple[str, ...]
    command_line: str
    working_directory: PureWindowsPath
    environment: tuple[tuple[str, str], ...]
    limits: WindowsJobLimits
    shell_kind: ResolvedShellKind | None

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("argv cannot be empty")
        if not self.command_line.strip():
            raise ValueError("command_line cannot be empty")
        if not isinstance(self.limits, WindowsJobLimits):
            raise TypeError("limits must be WindowsJobLimits")

    def environment_block(self) -> str:
        entries = [f"{name}={value}" for name, value in self.environment]
        return "\0".join(sorted(entries)) + "\0\0"


def job_limits_from(limits: ExecutionLimits) -> WindowsJobLimits:
    if not isinstance(limits, ExecutionLimits):
        raise TypeError("limits must be ExecutionLimits")
    return WindowsJobLimits(
        memory_bytes=limits.memory_bytes,
        cpu_seconds=limits.cpu_seconds,
        max_processes=limits.max_processes,
    )


def resolve_shell_program(
    shells: tuple[DiscoveredProgram, ...],
    kind: ResolvedShellKind,
) -> DiscoveredProgram:
    if not isinstance(shells, tuple):
        raise TypeError("shells must be a tuple")
    for shell in shells:
        if not isinstance(shell, DiscoveredProgram):
            raise TypeError("shells must contain DiscoveredProgram values")
        if shell.shell_kind == kind:
            return shell
    raise ValueError(f"no discovered {kind} shell is available")


def build_windows_plan(
    prepared: PreparedExecution,
    shells: tuple[DiscoveredProgram, ...] = (),
) -> WindowsLaunchPlan:
    if not isinstance(prepared, PreparedExecution):
        raise TypeError("prepared must be a PreparedExecution")

    request = prepared.request
    if request.filesystem_mode != "host":
        raise ValueError("the windows backend only supports the host filesystem mode")

    if request.mode == "exec":
        argv = tuple(request.argv or ())
        if not argv:
            raise ValueError("exec mode requires a non-empty argv")
        shell_kind = None
    else:
        script = request.script or ""
        if not script.strip():
            raise ValueError("shell mode requires a non-empty script")
        shell_kind = prepared.resolved_shell
        if shell_kind is None:
            raise ValueError("shell mode requires a resolved shell kind")
        shell_path = resolve_shell_program(shells, shell_kind).path
        if shell_kind == "powershell":
            argv = build_powershell_argv(str(shell_path), script)
        else:
            argv = build_command_shell_argv(str(shell_path), script)

    return WindowsLaunchPlan(
        argv=argv,
        command_line=build_command_line(argv),
        working_directory=PureWindowsPath(str(request.working_directory)),
        environment=tuple(sorted(prepared.environment.variables)),
        limits=job_limits_from(request.limits),
        shell_kind=shell_kind,
    )
