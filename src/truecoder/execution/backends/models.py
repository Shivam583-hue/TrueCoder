from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, TypeAlias

from ..models import (
    BACKEND_NAMES,
    RESOLVED_SHELL_KINDS,
    BackendCapabilities,
    BackendName,
    NativeDiagnostic,
    ResolvedShellKind,
)

FactState: TypeAlias = Literal["yes", "no", "unknown"]
HostSystem: TypeAlias = Literal["linux", "macos", "windows", "unknown"]
HostFamily: TypeAlias = Literal["posix", "windows", "unknown"]
ContainerRuntimeName: TypeAlias = Literal["docker", "podman", "nerdctl"]
OutputStreamName: TypeAlias = Literal["stdout", "stderr"]

FACT_STATES: Final = frozenset({"yes", "no", "unknown"})
HOST_SYSTEMS: Final = frozenset({"linux", "macos", "windows", "unknown"})
HOST_FAMILIES: Final = frozenset({"posix", "windows", "unknown"})
CONTAINER_RUNTIME_NAMES: Final = frozenset({"docker", "podman", "nerdctl"})
OUTPUT_STREAM_NAMES: Final = frozenset({"stdout", "stderr"})

MAX_DISCOVERY_IDENTIFIER_BYTES: Final = 128
MAX_DISCOVERY_VERSION_BYTES: Final = 1024
MAX_DISCOVERY_MESSAGE_BYTES: Final = 4096
MAX_DISCOVERY_DIAGNOSTIC_BYTES: Final = 4096
MAX_BACKEND_OUTPUT_CHUNK_BYTES: Final = 64 * 1024
MAX_CGROUP_CONTROLLERS: Final = 64


def _require_text(
    value: object,
    name: str,
    *,
    maximum: int,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain null bytes")
    size = len(value.encode("utf-8"))
    if size > maximum:
        raise ValueError(f"{name} must not exceed {maximum} UTF-8 bytes")
    return value


def _require_optional_text(
    value: object,
    name: str,
    *,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    return _require_text(value, name, maximum=maximum)


def _require_identifier(value: object, name: str) -> str:
    identifier = _require_text(
        value,
        name,
        maximum=MAX_DISCOVERY_IDENTIFIER_BYTES,
    )
    if identifier != identifier.strip():
        raise ValueError(f"{name} must not have surrounding whitespace")
    if (
        not identifier.isascii()
        or not identifier[0].isalnum()
        or any(
            not (character.isalnum() or character in "._:/-")
            for character in identifier
        )
    ):
        raise ValueError(f"{name} must be a stable ASCII identifier")
    return identifier


def _require_absolute_path(value: object, name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{name} must be a pathlib.Path")
    if "\x00" in str(value):
        raise ValueError(f"{name} must not contain null bytes")
    if not value.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return value.resolve(strict=False)


@dataclass(frozen=True, slots=True)
class HostPlatformInfo:
    system: HostSystem
    family: HostFamily
    architecture: str
    release: str | None = None

    def __post_init__(self) -> None:
        if self.system not in HOST_SYSTEMS:
            raise ValueError(f"unknown host system: {self.system!r}")
        if self.family not in HOST_FAMILIES:
            raise ValueError(f"unknown host family: {self.family!r}")
        expected_family: HostFamily = (
            "posix"
            if self.system in {"linux", "macos"}
            else "windows"
            if self.system == "windows"
            else "unknown"
        )
        if self.family != expected_family:
            raise ValueError(
                f"host system {self.system!r} requires family {expected_family!r}"
            )
        _require_text(
            self.architecture,
            "architecture",
            maximum=MAX_DISCOVERY_IDENTIFIER_BYTES,
        )
        _require_optional_text(
            self.release,
            "release",
            maximum=MAX_DISCOVERY_VERSION_BYTES,
        )


@dataclass(frozen=True, slots=True)
class DiscoveredProgram:
    name: str
    path: Path
    shell_kind: ResolvedShellKind
    version: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.name, "program name")
        object.__setattr__(
            self,
            "path",
            _require_absolute_path(self.path, "program path"),
        )
        if self.shell_kind not in RESOLVED_SHELL_KINDS:
            raise ValueError(f"unknown shell kind: {self.shell_kind!r}")
        _require_optional_text(
            self.version,
            "program version",
            maximum=MAX_DISCOVERY_VERSION_BYTES,
        )


@dataclass(frozen=True, slots=True)
class CgroupV2Info:
    mounted: bool
    writable: bool
    controllers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.mounted, bool):
            raise TypeError("mounted must be a boolean")
        if not isinstance(self.writable, bool):
            raise TypeError("writable must be a boolean")
        if not isinstance(self.controllers, tuple):
            raise TypeError("controllers must be a tuple")
        if len(self.controllers) > MAX_CGROUP_CONTROLLERS:
            raise ValueError("too many cgroup controllers")
        for index, controller in enumerate(self.controllers):
            _require_identifier(controller, f"controllers[{index}]")
        if len(self.controllers) != len(set(self.controllers)):
            raise ValueError("controllers must not contain duplicates")
        if tuple(sorted(self.controllers)) != self.controllers:
            raise ValueError("controllers must be sorted")
        if not self.mounted and (self.writable or self.controllers):
            raise ValueError(
                "an unmounted cgroup v2 filesystem cannot be writable "
                "or expose controllers"
            )


@dataclass(frozen=True, slots=True)
class ContainerRuntimeInfo:
    name: ContainerRuntimeName
    executable: Path
    client_version: str | None
    server_version: str | None
    daemon_reachable: bool
    rootless: FactState
    diagnostic: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.name not in CONTAINER_RUNTIME_NAMES:
            raise ValueError(f"unknown container runtime: {self.name!r}")
        object.__setattr__(
            self,
            "executable",
            _require_absolute_path(self.executable, "runtime executable"),
        )
        _require_optional_text(
            self.client_version,
            "client_version",
            maximum=MAX_DISCOVERY_VERSION_BYTES,
        )
        _require_optional_text(
            self.server_version,
            "server_version",
            maximum=MAX_DISCOVERY_VERSION_BYTES,
        )
        if not isinstance(self.daemon_reachable, bool):
            raise TypeError("daemon_reachable must be a boolean")
        if self.rootless not in FACT_STATES:
            raise ValueError(f"unknown rootless state: {self.rootless!r}")
        _require_optional_text(
            self.diagnostic,
            "runtime diagnostic",
            maximum=MAX_DISCOVERY_DIAGNOSTIC_BYTES,
        )
        if not self.daemon_reachable:
            if self.server_version is not None:
                raise ValueError("an unreachable runtime cannot have a server version")
            if self.rootless != "unknown":
                raise ValueError(
                    "an unreachable runtime must report rootless as unknown"
                )


@dataclass(frozen=True, slots=True)
class UnavailableReason:
    code: str
    message: str
    diagnostic: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _require_identifier(self.code, "reason code")
        _require_text(
            self.message,
            "reason message",
            maximum=MAX_DISCOVERY_MESSAGE_BYTES,
        )
        _require_optional_text(
            self.diagnostic,
            "reason diagnostic",
            maximum=MAX_DISCOVERY_DIAGNOSTIC_BYTES,
        )


@dataclass(frozen=True, slots=True)
class BackendDescriptor:
    name: BackendName
    available: bool
    capabilities: BackendCapabilities
    version: str | None = None
    runtime: ContainerRuntimeInfo | None = None
    unavailable_reasons: tuple[UnavailableReason, ...] = ()

    def __post_init__(self) -> None:
        if self.name not in BACKEND_NAMES:
            raise ValueError(f"unknown backend name: {self.name!r}")
        if not isinstance(self.available, bool):
            raise TypeError("available must be a boolean")
        if not isinstance(self.capabilities, BackendCapabilities):
            raise TypeError("capabilities must be BackendCapabilities")
        _require_optional_text(
            self.version,
            "backend version",
            maximum=MAX_DISCOVERY_VERSION_BYTES,
        )
        if self.runtime is not None and not isinstance(
            self.runtime,
            ContainerRuntimeInfo,
        ):
            raise TypeError("runtime must be ContainerRuntimeInfo or None")
        if not isinstance(self.unavailable_reasons, tuple):
            raise TypeError("unavailable_reasons must be a tuple")
        if any(
            not isinstance(reason, UnavailableReason)
            for reason in self.unavailable_reasons
        ):
            raise TypeError("unavailable_reasons must contain UnavailableReason values")
        reason_codes = tuple(reason.code for reason in self.unavailable_reasons)
        if len(reason_codes) != len(set(reason_codes)):
            raise ValueError("unavailable reason codes must not repeat")
        if self.available and self.unavailable_reasons:
            raise ValueError("an available backend cannot have unavailable reasons")
        if not self.available and not self.unavailable_reasons:
            raise ValueError("an unavailable backend must include a reason")
        if self.name == "container":
            if self.available and self.runtime is None:
                raise ValueError("an available container backend requires a runtime")
        elif self.runtime is not None:
            raise ValueError("local backends cannot contain a container runtime")


@dataclass(frozen=True, slots=True)
class DiscoverySnapshot:
    host: HostPlatformInfo
    shells: tuple[DiscoveredProgram, ...]
    cgroup_v2: CgroupV2Info | None
    runtimes: tuple[ContainerRuntimeInfo, ...]
    backends: tuple[BackendDescriptor, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.host, HostPlatformInfo):
            raise TypeError("host must be HostPlatformInfo")
        _require_tuple_of(self.shells, DiscoveredProgram, "shells")
        _require_tuple_of(self.runtimes, ContainerRuntimeInfo, "runtimes")
        _require_tuple_of(self.backends, BackendDescriptor, "backends")
        if self.cgroup_v2 is not None and not isinstance(
            self.cgroup_v2,
            CgroupV2Info,
        ):
            raise TypeError("cgroup_v2 must be CgroupV2Info or None")
        if self.host.system != "linux" and self.cgroup_v2 is not None:
            raise ValueError("cgroup_v2 information is Linux-specific")
        shell_names = tuple(shell.name for shell in self.shells)
        shell_paths = tuple(str(shell.path) for shell in self.shells)
        runtime_names = tuple(runtime.name for runtime in self.runtimes)
        backend_names = tuple(backend.name for backend in self.backends)
        for values, name in (
            (shell_names, "shell names"),
            (shell_paths, "shell paths"),
            (runtime_names, "runtime names"),
            (backend_names, "backend names"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not contain duplicates")
        if frozenset(backend_names) != BACKEND_NAMES:
            raise ValueError("a discovery snapshot must describe every backend")

    def backend(self, name: BackendName) -> BackendDescriptor:
        if name not in BACKEND_NAMES:
            raise ValueError(f"unknown backend name: {name!r}")
        return next(backend for backend in self.backends if backend.name == name)


@dataclass(frozen=True, slots=True)
class BackendCompatibility:
    backend: BackendName
    compatible: bool
    resolved_shell: ResolvedShellKind | None = None
    reasons: tuple[UnavailableReason, ...] = ()

    def __post_init__(self) -> None:
        if self.backend not in BACKEND_NAMES:
            raise ValueError(f"unknown backend name: {self.backend!r}")
        if not isinstance(self.compatible, bool):
            raise TypeError("compatible must be a boolean")
        if (
            self.resolved_shell is not None
            and self.resolved_shell not in RESOLVED_SHELL_KINDS
        ):
            raise ValueError(f"unknown resolved shell: {self.resolved_shell!r}")
        _require_tuple_of(self.reasons, UnavailableReason, "reasons")
        reason_codes = tuple(reason.code for reason in self.reasons)
        if len(reason_codes) != len(set(reason_codes)):
            raise ValueError("compatibility reason codes must not repeat")
        if self.compatible and self.reasons:
            raise ValueError("a compatible backend cannot have reasons")
        if not self.compatible and not self.reasons:
            raise ValueError("an incompatible backend must include reasons")
        if not self.compatible and self.resolved_shell is not None:
            raise ValueError("an incompatible backend cannot have a resolved shell")


@dataclass(frozen=True, slots=True)
class SelectedBackend:
    descriptor: BackendDescriptor
    resolved_shell: ResolvedShellKind | None
    selection_reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, BackendDescriptor):
            raise TypeError("descriptor must be a BackendDescriptor")
        if not self.descriptor.available:
            raise ValueError("cannot select an unavailable backend")
        if (
            self.resolved_shell is not None
            and self.resolved_shell not in self.descriptor.capabilities.supported_shells
        ):
            raise ValueError("resolved shell is not supported by the backend")
        _require_text(
            self.selection_reason,
            "selection_reason",
            maximum=MAX_DISCOVERY_MESSAGE_BYTES,
        )


@dataclass(frozen=True, slots=True)
class BackendOutputChunk:
    stream: OutputStreamName
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if self.stream not in OUTPUT_STREAM_NAMES:
            raise ValueError(f"unknown output stream: {self.stream!r}")
        if not isinstance(self.data, bytes):
            raise TypeError("data must be bytes")
        if not self.data:
            raise ValueError("data must not be empty")
        if len(self.data) > MAX_BACKEND_OUTPUT_CHUNK_BYTES:
            raise ValueError(
                f"data must not exceed {MAX_BACKEND_OUTPUT_CHUNK_BYTES} bytes"
            )


@dataclass(frozen=True, slots=True)
class BackendExit:
    exit_code: int | None
    native_reason: str | None = None

    def __post_init__(self) -> None:
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise TypeError("exit_code must be an integer or None")
        _require_optional_text(
            self.native_reason,
            "native_reason",
            maximum=MAX_DISCOVERY_MESSAGE_BYTES,
        )
        if self.exit_code is None and self.native_reason is None:
            raise ValueError("an exit without a code must include a native reason")


@dataclass(frozen=True, slots=True)
class CleanupResult:
    complete: bool
    diagnostic: NativeDiagnostic | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be a boolean")
        if self.diagnostic is not None and not isinstance(
            self.diagnostic,
            NativeDiagnostic,
        ):
            raise TypeError("diagnostic must be NativeDiagnostic or None")
        if self.complete and self.diagnostic is not None:
            raise ValueError("complete cleanup cannot include a diagnostic")
        if not self.complete and self.diagnostic is None:
            raise ValueError("incomplete cleanup requires a diagnostic")


def _require_tuple_of(
    values: object,
    value_type: type[object],
    name: str,
) -> tuple[object, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    if any(not isinstance(value, value_type) for value in values):
        raise TypeError(f"{name} must contain {value_type.__name__} values")
    return values
