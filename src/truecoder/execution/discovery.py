from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol, TypeAlias

from .backends.models import (
    MAX_DISCOVERY_DIAGNOSTIC_BYTES,
    BackendDescriptor,
    CgroupV2Info,
    ContainerRuntimeInfo,
    DiscoveredProgram,
    DiscoverySnapshot,
    FactState,
    HostPlatformInfo,
    HostSystem,
    UnavailableReason,
)
from .models import BackendCapabilities, ResolvedShellKind
from .output import TerminalSanitizer

ProbeStatus: TypeAlias = Literal[
    "completed",
    "failed",
    "timed_out",
    "output_limit",
    "failed_to_start",
]

PROBE_STATUSES: Final = frozenset(
    {
        "completed",
        "failed",
        "timed_out",
        "output_limit",
        "failed_to_start",
    }
)
PROBE_TIMEOUT_SECONDS: Final = 3.0
PROBE_OUTPUT_LIMIT_BYTES: Final = 32 * 1024
CGROUP_READ_LIMIT_BYTES: Final = 16 * 1024

_CGROUP_ROOT: Final = Path("/sys/fs/cgroup")
_CGROUP_CONTROLLERS: Final = _CGROUP_ROOT / "cgroup.controllers"
_PROC_SELF_CGROUP: Final = Path("/proc/self/cgroup")
_VERSION_PATTERN = re.compile(r"\b\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z._-]+)?\b")

_SHELL_CANDIDATES: Final = {
    "posix": (
        ("sh", "posix"),
        ("bash", "posix"),
        ("zsh", "posix"),
        ("pwsh", "powershell"),
    ),
    "windows": (
        ("pwsh.exe", "powershell"),
        ("powershell.exe", "powershell"),
        ("cmd.exe", None),
    ),
    "unknown": (),
}

_RUNTIME_CANDIDATES: Final = (
    ("docker", "docker"),
    ("podman", "podman"),
    ("nerdctl", "nerdctl"),
)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: ProbeStatus
    exit_code: int | None
    output: str = field(default="", repr=False)
    diagnostic: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.status not in PROBE_STATUSES:
            raise ValueError(f"unknown probe status: {self.status!r}")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise TypeError("exit_code must be an integer or None")
        if not isinstance(self.output, str):
            raise TypeError("output must be a string")
        if len(self.output.encode("utf-8")) > PROBE_OUTPUT_LIMIT_BYTES:
            raise ValueError("probe output exceeds its model limit")
        if self.diagnostic is not None:
            if not isinstance(self.diagnostic, str):
                raise TypeError("diagnostic must be a string or None")
            if not self.diagnostic.strip():
                raise ValueError("diagnostic must not be empty")
            if len(self.diagnostic.encode("utf-8")) > MAX_DISCOVERY_DIAGNOSTIC_BYTES:
                raise ValueError("probe diagnostic is too large")
        if self.status == "completed":
            if self.exit_code != 0:
                raise ValueError("a completed probe requires exit_code=0")
            if self.diagnostic is not None:
                raise ValueError("a completed probe cannot include a diagnostic")
        elif self.status == "failed":
            if self.exit_code in {None, 0}:
                raise ValueError("a failed probe requires a nonzero exit code")
        elif self.exit_code is not None:
            raise ValueError(f"{self.status} probes cannot include an exit code")


class DiscoveryIO(Protocol):
    def platform_system(self) -> str: ...

    def platform_machine(self) -> str: ...

    def platform_release(self) -> str: ...

    def which(self, executable: str) -> Path | None: ...

    def path_exists(self, path: Path) -> bool: ...

    def path_writable(self, path: Path) -> bool: ...

    def read_text(self, path: Path, maximum_bytes: int) -> str: ...

    async def run_probe(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> ProbeResult: ...


class _ProbeOutputExceeded(Exception):
    def __init__(self, retained: bytes) -> None:
        self.retained = retained
        super().__init__("probe output limit exceeded")


class SystemDiscoveryIO:
    """The only Phase 5 object allowed to inspect the real host."""

    def platform_system(self) -> str:
        return platform.system()

    def platform_machine(self) -> str:
        return platform.machine()

    def platform_release(self) -> str:
        return platform.release()

    def which(self, executable: str) -> Path | None:
        resolved = shutil.which(executable)
        if resolved is None:
            return None
        return Path(resolved).resolve(strict=False)

    def path_exists(self, path: Path) -> bool:
        return path.exists()

    def path_writable(self, path: Path) -> bool:
        return os.access(path, os.W_OK)

    def read_text(self, path: Path, maximum_bytes: int) -> str:
        _require_positive_int(maximum_bytes, "maximum_bytes")
        with path.open("rb") as file:
            data = file.read(maximum_bytes + 1)
        if len(data) > maximum_bytes:
            raise ValueError(f"{path} exceeds the discovery read limit")
        return data.decode("utf-8", errors="replace")

    async def run_probe(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> ProbeResult:
        _validate_probe_request(argv, timeout_seconds, max_output_bytes)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=_probe_environment(),
            )
        except OSError as exc:
            return ProbeResult(
                status="failed_to_start",
                exit_code=None,
                diagnostic=_bounded_diagnostic(str(exc)),
            )

        async def collect() -> tuple[int, bytes]:
            if process.stdout is None:
                raise RuntimeError("probe stdout pipe was not created")
            output = bytearray()
            while chunk := await process.stdout.read(4096):
                remaining = max_output_bytes - len(output)
                if len(chunk) > remaining:
                    output.extend(chunk[:remaining])
                    raise _ProbeOutputExceeded(bytes(output))
                output.extend(chunk)
            return await process.wait(), bytes(output)

        task = asyncio.create_task(collect())
        try:
            exit_code, raw_output = await asyncio.wait_for(
                task,
                timeout=timeout_seconds,
            )
        except TimeoutError:
            await _kill_probe(process, task)
            return ProbeResult(
                status="timed_out",
                exit_code=None,
                diagnostic="probe exceeded its timeout",
            )
        except _ProbeOutputExceeded as exc:
            await _kill_probe(process, task)
            return ProbeResult(
                status="output_limit",
                exit_code=None,
                output=_sanitize_probe_output(exc.retained),
                diagnostic="probe exceeded its output limit",
            )

        output = _sanitize_probe_output(raw_output)
        if exit_code == 0:
            return ProbeResult(
                status="completed",
                exit_code=0,
                output=output,
            )
        return ProbeResult(
            status="failed",
            exit_code=exit_code,
            output=output,
            diagnostic=_bounded_diagnostic(
                output or f"probe exited with status {exit_code}"
            ),
        )


async def discover_execution_environment(
    io: DiscoveryIO | None = None,
) -> DiscoverySnapshot:
    discovery_io = io or SystemDiscoveryIO()
    host = discover_host(discovery_io)
    shells = await discover_shells(host, discovery_io)
    cgroup_v2 = discover_cgroup_v2(host, discovery_io)
    runtimes = await discover_container_runtimes(discovery_io)
    return DiscoverySnapshot(
        host=host,
        shells=shells,
        cgroup_v2=cgroup_v2,
        runtimes=runtimes,
        backends=derive_backend_descriptors(
            host=host,
            shells=shells,
            cgroup_v2=cgroup_v2,
            runtimes=runtimes,
        ),
    )


def discover_host(io: DiscoveryIO) -> HostPlatformInfo:
    raw_system = io.platform_system().strip().casefold()
    system: HostSystem = (
        "linux"
        if raw_system == "linux"
        else "macos"
        if raw_system in {"darwin", "macos"}
        else "windows"
        if raw_system == "windows"
        else "unknown"
    )
    family = (
        "posix"
        if system in {"linux", "macos"}
        else "windows"
        if system == "windows"
        else "unknown"
    )
    architecture = io.platform_machine().strip() or "unknown"
    release = io.platform_release().strip() or None
    return HostPlatformInfo(
        system=system,
        family=family,
        architecture=architecture,
        release=release,
    )


async def discover_shells(
    host: HostPlatformInfo,
    io: DiscoveryIO,
) -> tuple[DiscoveredProgram, ...]:
    candidates = _SHELL_CANDIDATES[host.family]
    discovered: list[DiscoveredProgram] = []
    seen_paths: set[str] = set()
    for executable, shell_kind in candidates:
        path = io.which(executable)
        if path is None:
            continue
        canonical = path.resolve(strict=False)
        path_key = (
            str(canonical).casefold() if host.family == "windows" else str(canonical)
        )
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        version = await _discover_shell_version(
            executable,
            canonical,
            io,
        )
        discovered.append(
            DiscoveredProgram(
                name=executable.casefold().removesuffix(".exe"),
                path=canonical,
                shell_kind=shell_kind,
                version=version,
            )
        )
    return tuple(discovered)


def discover_cgroup_v2(
    host: HostPlatformInfo,
    io: DiscoveryIO,
) -> CgroupV2Info | None:
    if host.system != "linux":
        return None
    if not io.path_exists(_CGROUP_CONTROLLERS):
        return CgroupV2Info(mounted=False, writable=False)

    controller_text = io.read_text(
        _CGROUP_CONTROLLERS,
        CGROUP_READ_LIMIT_BYTES,
    )
    controllers = tuple(
        sorted(
            {
                controller
                for controller in controller_text.split()
                if _valid_discovery_identifier(controller)
            }
        )
    )
    delegated_path = _discover_delegated_cgroup_path(io)
    return CgroupV2Info(
        mounted=True,
        writable=io.path_writable(delegated_path),
        controllers=controllers,
    )


async def discover_container_runtimes(
    io: DiscoveryIO,
) -> tuple[ContainerRuntimeInfo, ...]:
    runtimes: list[ContainerRuntimeInfo] = []
    for runtime_name, executable_name in _RUNTIME_CANDIDATES:
        executable = io.which(executable_name)
        if executable is None:
            continue
        canonical = executable.resolve(strict=False)
        runtimes.append(
            await _inspect_container_runtime(
                runtime_name,
                canonical,
                io,
            )
        )
    return tuple(runtimes)


def derive_backend_descriptors(
    *,
    host: HostPlatformInfo,
    shells: tuple[DiscoveredProgram, ...],
    cgroup_v2: CgroupV2Info | None,
    runtimes: tuple[ContainerRuntimeInfo, ...],
) -> tuple[BackendDescriptor, ...]:
    shell_kinds = frozenset(
        shell.shell_kind for shell in shells if shell.shell_kind is not None
    )
    posix_shells = ("posix",) if "posix" in shell_kinds else ()
    powershell_shells = ("powershell",) if "powershell" in shell_kinds else ()

    posix_available = host.family == "posix"
    posix_capabilities = _posix_capabilities(
        shell_kinds=posix_shells,
        cgroup_v2=cgroup_v2,
    )
    posix = BackendDescriptor(
        name="posix",
        available=posix_available,
        capabilities=posix_capabilities,
        version=host.release if posix_available else None,
        unavailable_reasons=(
            ()
            if posix_available
            else (
                UnavailableReason(
                    code="host-not-posix",
                    message="The local host is not a supported POSIX system.",
                ),
            )
        ),
    )

    windows_available = host.family == "windows"
    windows_capabilities = _windows_capabilities(
        shell_kinds=powershell_shells,
    )
    windows = BackendDescriptor(
        name="windows",
        available=windows_available,
        capabilities=windows_capabilities,
        version=host.release if windows_available else None,
        unavailable_reasons=(
            ()
            if windows_available
            else (
                UnavailableReason(
                    code="host-not-windows",
                    message="The local host is not Windows.",
                ),
            )
        ),
    )

    usable_runtime = next(
        (
            runtime
            for runtime in runtimes
            if runtime.daemon_reachable and runtime.diagnostic is None
        ),
        None,
    )
    container = BackendDescriptor(
        name="container",
        available=usable_runtime is not None,
        capabilities=_container_capabilities(),
        version=(
            usable_runtime.server_version or usable_runtime.client_version
            if usable_runtime is not None
            else None
        ),
        runtime=usable_runtime,
        unavailable_reasons=(
            ()
            if usable_runtime is not None
            else _container_unavailable_reasons(runtimes)
        ),
    )
    return posix, windows, container


async def _discover_shell_version(
    executable_name: str,
    executable: Path,
    io: DiscoveryIO,
) -> str | None:
    normalized = executable_name.casefold()
    if normalized in {"bash", "zsh"}:
        argv = (str(executable), "--version")
    elif normalized in {"pwsh", "pwsh.exe", "powershell.exe"}:
        argv = (
            str(executable),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        )
    elif normalized == "cmd.exe":
        argv = (str(executable), "/d", "/c", "ver")
    else:
        return None
    result = await io.run_probe(
        argv,
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
        max_output_bytes=PROBE_OUTPUT_LIMIT_BYTES,
    )
    if result.status != "completed":
        return None
    return _extract_version(result.output)


async def _inspect_container_runtime(
    runtime_name: str,
    executable: Path,
    io: DiscoveryIO,
) -> ContainerRuntimeInfo:
    client_result = await io.run_probe(
        (str(executable), "--version"),
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
        max_output_bytes=PROBE_OUTPUT_LIMIT_BYTES,
    )
    client_version = (
        _extract_version(client_result.output)
        if client_result.status == "completed"
        else None
    )
    if runtime_name == "docker":
        return await _inspect_docker(
            executable,
            client_version,
            io,
        )
    if runtime_name == "podman":
        return await _inspect_podman(
            executable,
            client_version,
            io,
        )
    return await _inspect_nerdctl(
        executable,
        client_version,
        io,
    )


async def _inspect_docker(
    executable: Path,
    client_version: str | None,
    io: DiscoveryIO,
) -> ContainerRuntimeInfo:
    version_result = await io.run_probe(
        (str(executable), "version", "--format", "{{json .}}"),
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
        max_output_bytes=PROBE_OUTPUT_LIMIT_BYTES,
    )
    if version_result.status != "completed":
        return _unreachable_runtime(
            "docker",
            executable,
            client_version,
            version_result,
        )
    data = _load_json_object(version_result.output)
    if data is None:
        return _inspection_failed_runtime(
            "docker",
            executable,
            client_version,
            "Docker returned invalid version metadata.",
        )
    server_version = _nested_string(data, "Server", "Version")
    discovered_client = _nested_string(data, "Client", "Version")
    info_result = await io.run_probe(
        (
            str(executable),
            "info",
            "--format",
            "{{json .SecurityOptions}}",
        ),
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
        max_output_bytes=PROBE_OUTPUT_LIMIT_BYTES,
    )
    rootless = (
        _docker_rootless(info_result.output)
        if info_result.status == "completed"
        else "unknown"
    )
    return ContainerRuntimeInfo(
        name="docker",
        executable=executable,
        client_version=discovered_client or client_version,
        server_version=server_version,
        daemon_reachable=True,
        rootless=rootless,
    )


async def _inspect_podman(
    executable: Path,
    client_version: str | None,
    io: DiscoveryIO,
) -> ContainerRuntimeInfo:
    info_result = await io.run_probe(
        (str(executable), "info", "--format", "json"),
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
        max_output_bytes=PROBE_OUTPUT_LIMIT_BYTES,
    )
    if info_result.status != "completed":
        return _unreachable_runtime(
            "podman",
            executable,
            client_version,
            info_result,
        )
    data = _load_json_object(info_result.output)
    if data is None:
        return _inspection_failed_runtime(
            "podman",
            executable,
            client_version,
            "Podman returned invalid info metadata.",
        )
    return ContainerRuntimeInfo(
        name="podman",
        executable=executable,
        client_version=client_version,
        server_version=(
            _nested_string(data, "version", "Version")
            or _nested_string(data, "version", "version")
        ),
        daemon_reachable=True,
        rootless=_nested_rootless(data),
    )


async def _inspect_nerdctl(
    executable: Path,
    client_version: str | None,
    io: DiscoveryIO,
) -> ContainerRuntimeInfo:
    info_result = await io.run_probe(
        (str(executable), "info", "--format", "{{json .}}"),
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
        max_output_bytes=PROBE_OUTPUT_LIMIT_BYTES,
    )
    if info_result.status != "completed":
        return _unreachable_runtime(
            "nerdctl",
            executable,
            client_version,
            info_result,
        )
    data = _load_json_object(info_result.output)
    if data is None:
        return _inspection_failed_runtime(
            "nerdctl",
            executable,
            client_version,
            "nerdctl returned invalid info metadata.",
        )
    return ContainerRuntimeInfo(
        name="nerdctl",
        executable=executable,
        client_version=client_version,
        server_version=(
            _nested_string(data, "ServerVersion")
            or _nested_string(data, "serverVersion")
        ),
        daemon_reachable=True,
        rootless=_nested_rootless(data),
    )


def _unreachable_runtime(
    name: str,
    executable: Path,
    client_version: str | None,
    result: ProbeResult,
) -> ContainerRuntimeInfo:
    return ContainerRuntimeInfo(
        name=name,  # type: ignore[arg-type]
        executable=executable,
        client_version=client_version,
        server_version=None,
        daemon_reachable=False,
        rootless="unknown",
        diagnostic=result.diagnostic or "container runtime is unreachable",
    )


def _inspection_failed_runtime(
    name: str,
    executable: Path,
    client_version: str | None,
    diagnostic: str,
) -> ContainerRuntimeInfo:
    return ContainerRuntimeInfo(
        name=name,  # type: ignore[arg-type]
        executable=executable,
        client_version=client_version,
        server_version=None,
        daemon_reachable=True,
        rootless="unknown",
        diagnostic=diagnostic,
    )


def _discover_delegated_cgroup_path(io: DiscoveryIO) -> Path:
    if not io.path_exists(_PROC_SELF_CGROUP):
        return _CGROUP_ROOT
    try:
        membership = io.read_text(
            _PROC_SELF_CGROUP,
            CGROUP_READ_LIMIT_BYTES,
        )
    except (OSError, ValueError):
        return _CGROUP_ROOT
    relative = next(
        (
            line.partition("::")[2].strip().lstrip("/")
            for line in membership.splitlines()
            if "::" in line
        ),
        "",
    )
    candidate = (_CGROUP_ROOT / relative).resolve(strict=False)
    try:
        candidate.relative_to(_CGROUP_ROOT)
    except ValueError:
        return _CGROUP_ROOT
    return candidate


def _posix_capabilities(
    *,
    shell_kinds: tuple[ResolvedShellKind, ...],
    cgroup_v2: CgroupV2Info | None,
) -> BackendCapabilities:
    controllers = (
        frozenset(cgroup_v2.controllers)
        if cgroup_v2 is not None and cgroup_v2.mounted and cgroup_v2.writable
        else frozenset()
    )
    execution_modes = ("exec", "shell") if shell_kinds else ("exec",)
    return BackendCapabilities(
        filesystem_isolation="unsupported",
        network_isolation="unsupported",
        memory_limits="enforced" if "memory" in controllers else "best_effort",
        cpu_limits="enforced" if "cpu" in controllers else "best_effort",
        process_limits="enforced" if "pids" in controllers else "best_effort",
        timeout_enforcement="enforced",
        cancellation="enforced",
        supported_execution_modes=execution_modes,
        supported_filesystem_modes=("host",),
        supported_shells=shell_kinds,
    )


def _windows_capabilities(
    *,
    shell_kinds: tuple[ResolvedShellKind, ...],
) -> BackendCapabilities:
    execution_modes = ("exec", "shell") if shell_kinds else ("exec",)
    return BackendCapabilities(
        filesystem_isolation="unsupported",
        network_isolation="unsupported",
        memory_limits="enforced",
        cpu_limits="enforced",
        process_limits="enforced",
        timeout_enforcement="enforced",
        cancellation="enforced",
        supported_execution_modes=execution_modes,
        supported_filesystem_modes=("host",),
        supported_shells=shell_kinds,
    )


def _container_capabilities() -> BackendCapabilities:
    return BackendCapabilities(
        filesystem_isolation="enforced",
        network_isolation="enforced",
        memory_limits="enforced",
        cpu_limits="enforced",
        process_limits="enforced",
        timeout_enforcement="enforced",
        cancellation="enforced",
        supported_execution_modes=("exec", "shell"),
        supported_filesystem_modes=("workspace-read", "workspace-write"),
        supported_shells=("posix",),
    )


def _container_unavailable_reasons(
    runtimes: tuple[ContainerRuntimeInfo, ...],
) -> tuple[UnavailableReason, ...]:
    if not runtimes:
        return (
            UnavailableReason(
                code="container-runtime-not-found",
                message="No supported container runtime client was found.",
            ),
        )
    if any(runtime.daemon_reachable for runtime in runtimes):
        diagnostics = "; ".join(
            f"{runtime.name}: {runtime.diagnostic}"
            for runtime in runtimes
            if runtime.diagnostic is not None
        )
        return (
            UnavailableReason(
                code="container-runtime-inspection-failed",
                message=(
                    "A container runtime responded, but its capabilities "
                    "could not be verified."
                ),
                diagnostic=_bounded_diagnostic(diagnostics),
            ),
        )
    diagnostics = "; ".join(
        f"{runtime.name}: {runtime.diagnostic}"
        for runtime in runtimes
        if runtime.diagnostic is not None
    )
    return (
        UnavailableReason(
            code="container-runtime-unreachable",
            message="Installed container runtime services are unreachable.",
            diagnostic=_bounded_diagnostic(diagnostics),
        ),
    )


def _docker_rootless(output: str) -> FactState:
    try:
        values = json.loads(output)
    except json.JSONDecodeError:
        return "unknown"
    if not isinstance(values, list):
        return "unknown"
    normalized = tuple(str(value).casefold() for value in values)
    return (
        "yes"
        if any(
            value == "rootless"
            or value == "name=rootless"
            or "rootless" in value.split("=")
            for value in normalized
        )
        else "no"
    )


def _nested_rootless(value: object) -> FactState:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).casefold() == "rootless" and isinstance(nested, bool):
                return "yes" if nested else "no"
        for nested in value.values():
            result = _nested_rootless(nested)
            if result != "unknown":
                return result
    elif isinstance(value, list):
        for nested in value:
            result = _nested_rootless(nested)
            if result != "unknown":
                return result
    return "unknown"


def _load_json_object(output: str) -> dict[str, object] | None:
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _nested_string(
    value: Mapping[str, object],
    *path: str,
) -> str | None:
    current: object = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current.strip() if isinstance(current, str) and current.strip() else None


def _extract_version(output: str) -> str | None:
    match = _VERSION_PATTERN.search(output)
    if match is not None:
        return match.group(0)
    first_line = output.strip().splitlines()[0] if output.strip() else ""
    return first_line[:256] or None


def _valid_discovery_identifier(value: str) -> bool:
    return (
        bool(value)
        and value.isascii()
        and all(character.isalnum() or character in "._:/-" for character in value)
    )


async def _kill_probe(
    process: asyncio.subprocess.Process,
    task: asyncio.Task[tuple[int, bytes]],
) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    await process.wait()
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, _ProbeOutputExceeded):
        pass


def _sanitize_probe_output(raw: bytes) -> str:
    decoded = raw.decode("utf-8", errors="replace")
    sanitized = TerminalSanitizer().feed(decoded, final=True).strip()
    return _bound_utf8(sanitized, PROBE_OUTPUT_LIMIT_BYTES)


def _bounded_diagnostic(value: str) -> str:
    sanitized = TerminalSanitizer().feed(value, final=True).strip()
    bounded = _bound_utf8(sanitized, MAX_DISCOVERY_DIAGNOSTIC_BYTES)
    return bounded or "discovery probe failed"


def _bound_utf8(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", errors="ignore")


def _probe_environment() -> dict[str, str]:
    allowed = (
        ("ComSpec", "PATHEXT", "SystemRoot", "TEMP", "TMP")
        if os.name == "nt"
        else ("LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")
    )
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    if os.name != "nt":
        environment.setdefault("LANG", "C")
        environment.setdefault("LC_ALL", "C")
    return environment


def _validate_probe_request(
    argv: object,
    timeout_seconds: object,
    max_output_bytes: object,
) -> None:
    if not isinstance(argv, tuple) or not argv:
        raise ValueError("probe argv must be a non-empty tuple")
    for index, argument in enumerate(argv):
        if not isinstance(argument, str):
            raise TypeError(f"probe argv[{index}] must be a string")
        if not argument or "\x00" in argument:
            raise ValueError(f"probe argv[{index}] is invalid")
    if not Path(argv[0]).is_absolute():
        raise ValueError("probe executable must be an absolute path")
    if isinstance(timeout_seconds, bool) or not isinstance(
        timeout_seconds,
        (int, float),
    ):
        raise TypeError("timeout_seconds must be a number")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    _require_positive_int(max_output_bytes, "max_output_bytes")
    if max_output_bytes > PROBE_OUTPUT_LIMIT_BYTES:
        raise ValueError(f"max_output_bytes must not exceed {PROBE_OUTPUT_LIMIT_BYTES}")


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value
