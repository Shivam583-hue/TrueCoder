from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Literal, TypeAlias

from ..models import BackendCapabilities, CapabilityLevel
from .models import ContainerRuntimeName, UnavailableReason

ContainerState: TypeAlias = Literal[
    "created",
    "running",
    "paused",
    "restarting",
    "removing",
    "exited",
    "dead",
]
NetworkMode: TypeAlias = Literal["none", "isolated"]
ContainerPlatform: TypeAlias = Literal["linux/amd64", "linux/arm64"]

CONTAINER_STATES: Final = frozenset(
    {
        "created",
        "running",
        "paused",
        "restarting",
        "removing",
        "exited",
        "dead",
    }
)
NETWORK_MODES: Final = frozenset({"none", "isolated"})
CONTAINER_PLATFORMS: Final = frozenset({"linux/amd64", "linux/arm64"})
TERMINAL_CONTAINER_STATES: Final = frozenset({"exited", "dead"})

LABEL_NAMESPACE: Final = "ai.truecoder"
LABEL_MANAGED: Final = f"{LABEL_NAMESPACE}.managed"
LABEL_EXECUTION_ID: Final = f"{LABEL_NAMESPACE}.execution-id"
LABEL_AUDIT_RUN_ID: Final = f"{LABEL_NAMESPACE}.audit-run-id"
LABEL_OWNERSHIP_TOKEN: Final = f"{LABEL_NAMESPACE}.ownership-token"
LABEL_SCHEMA: Final = f"{LABEL_NAMESPACE}.label-schema"
LABEL_IMAGE_DIGEST: Final = f"{LABEL_NAMESPACE}.image-digest"

LABEL_SCHEMA_VERSION: Final = "1"
PLAN_VERSION: Final = "1"
CONTAINER_WORKSPACE: Final = PurePosixPath("/workspace")

MAX_NAME_LENGTH: Final = 96
MAX_LABEL_VALUE_BYTES: Final = 512
MAX_IDENTIFIER_BYTES: Final = 128
MAX_DIAGNOSTIC_BYTES: Final = 4096
MAX_MOUNTS: Final = 4
MAX_TMPFS: Final = 6
MAX_ARGV: Final = 4096
MAX_ARGUMENT_BYTES: Final = 4096

MIN_MEMORY_BYTES: Final = 6 * 1024 * 1024
MIN_PIDS_LIMIT: Final = 8

_DIGEST_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_NUMERIC_USER_PATTERN: Final = re.compile(r"^[0-9]+:[0-9]+$")

FORBIDDEN_MOUNT_SOURCES: Final = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/var/run/podman/podman.sock",
    "/run/podman/podman.sock",
    "/run/containerd/containerd.sock",
    "/var/run/containerd/containerd.sock",
    "/dev",
    "/proc",
    "/sys",
    "/boot",
    "/etc",
    "/root",
)

APPROVED_TMPFS_TARGETS: Final = frozenset(
    {"/tmp", "/run", "/home/truecoder", "/var/tmp"}
)


def _require_text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain null bytes")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} UTF-8 bytes")
    return value


def _require_identifier(value: object, name: str) -> str:
    text = _require_text(value, name, maximum=MAX_IDENTIFIER_BYTES)
    if text != text.strip():
        raise ValueError(f"{name} must not have surrounding whitespace")
    if not text.isascii():
        raise ValueError(f"{name} must be ASCII")
    if any(character.isspace() for character in text):
        raise ValueError(f"{name} must not contain whitespace")
    return text


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _require_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _require_canonical_directory(value: object, name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{name} must be a pathlib.Path")
    if "\x00" in str(value):
        raise ValueError(f"{name} must not contain null bytes")
    if not value.is_absolute():
        raise ValueError(f"{name} must be absolute")
    if value != Path(*value.parts):
        raise ValueError(f"{name} must be canonical")
    return value


def _require_posix_target(value: object, name: str) -> PurePosixPath:
    if not isinstance(value, PurePosixPath):
        raise TypeError(f"{name} must be a PurePosixPath")
    text = str(value)
    if "\x00" in text:
        raise ValueError(f"{name} must not contain null bytes")
    if not value.is_absolute():
        raise ValueError(f"{name} must be absolute")
    if ".." in value.parts:
        raise ValueError(f"{name} must not traverse upwards")
    return value


@dataclass(frozen=True, slots=True)
class ContainerImage:
    reference: str
    digest: str
    platform: ContainerPlatform
    user: str
    entrypoint_version: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.reference, "image reference")
        _require_identifier(self.digest, "image digest")
        if not _DIGEST_PATTERN.match(self.digest):
            raise ValueError("image digest must be a full sha256 digest")
        if self.digest not in self.reference:
            raise ValueError("image reference must be pinned to its digest")
        if self.platform not in CONTAINER_PLATFORMS:
            raise ValueError(f"unsupported image platform: {self.platform!r}")
        _require_identifier(self.user, "image user")
        if not _NUMERIC_USER_PATTERN.match(self.user):
            raise ValueError("image user must be numeric uid:gid")
        if self.user.split(":", 1)[0] == "0":
            raise ValueError("image user must not be root")
        if self.entrypoint_version is not None:
            _require_identifier(self.entrypoint_version, "entrypoint version")

    @property
    def uid(self) -> int:
        return int(self.user.split(":", 1)[0])

    @property
    def gid(self) -> int:
        return int(self.user.split(":", 1)[1])


@dataclass(frozen=True, slots=True)
class ContainerLabels:
    execution_id: str
    audit_run_id: str
    ownership_token: str
    image_digest: str
    schema_version: str = LABEL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("execution_id", self.execution_id),
            ("audit_run_id", self.audit_run_id),
            ("ownership_token", self.ownership_token),
            ("image_digest", self.image_digest),
            ("schema_version", self.schema_version),
        ):
            _require_identifier(value, name)
            if len(value.encode("utf-8")) > MAX_LABEL_VALUE_BYTES:
                raise ValueError(f"{name} exceeds the label value limit")
        if not _DIGEST_PATTERN.match(self.image_digest):
            raise ValueError("image_digest must be a full sha256 digest")

    def as_pairs(self) -> tuple[tuple[str, str], ...]:
        return (
            (LABEL_MANAGED, "true"),
            (LABEL_EXECUTION_ID, self.execution_id),
            (LABEL_AUDIT_RUN_ID, self.audit_run_id),
            (LABEL_OWNERSHIP_TOKEN, self.ownership_token),
            (LABEL_IMAGE_DIGEST, self.image_digest),
            (LABEL_SCHEMA, self.schema_version),
        )

    def matches(self, labels: dict[str, str]) -> bool:
        if labels.get(LABEL_MANAGED) != "true":
            return False
        return all(key in labels and labels[key] == value for key, value in self.as_pairs())


@dataclass(frozen=True, slots=True)
class ContainerMount:
    source: Path
    target: PurePosixPath
    read_only: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source",
            _require_canonical_directory(self.source, "mount source"),
        )
        object.__setattr__(
            self,
            "target",
            _require_posix_target(self.target, "mount target"),
        )
        _require_bool(self.read_only, "read_only")

        source = str(self.source)
        for forbidden in FORBIDDEN_MOUNT_SOURCES:
            if source == forbidden or source.startswith(f"{forbidden}/"):
                raise ValueError(f"mount source is forbidden: {source}")
        if any(character in source for character in (",", "=")):
            raise ValueError("mount source contains unencodable delimiters")
        if any(character in str(self.target) for character in (",", ":", "=")):
            raise ValueError("mount target contains unencodable delimiters")


@dataclass(frozen=True, slots=True)
class ContainerTmpfs:
    target: PurePosixPath
    size_bytes: int
    uid: int
    gid: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target",
            _require_posix_target(self.target, "tmpfs target"),
        )
        if str(self.target) not in APPROVED_TMPFS_TARGETS:
            raise ValueError(f"tmpfs target is not approved: {self.target}")
        _require_positive_int(self.size_bytes, "tmpfs size_bytes")
        for name, value in (("uid", self.uid), ("gid", self.gid)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"tmpfs {name} must be an integer")
            if value <= 0:
                raise ValueError(f"tmpfs {name} must not be root")


@dataclass(frozen=True, slots=True)
class ContainerSecurityProfile:
    memory_bytes: int
    pids_limit: int
    cpu_rate: float | None = None
    network_mode: NetworkMode = "none"
    read_only_root: bool = True
    drop_all_capabilities: bool = True
    no_new_privileges: bool = True
    seccomp_profile: str = "builtin"
    tmpfs: tuple[ContainerTmpfs, ...] = ()

    def __post_init__(self) -> None:
        _require_positive_int(self.memory_bytes, "memory_bytes")
        if self.memory_bytes < MIN_MEMORY_BYTES:
            raise ValueError(
                f"memory_bytes must be at least {MIN_MEMORY_BYTES} bytes"
            )
        _require_positive_int(self.pids_limit, "pids_limit")
        if self.pids_limit < MIN_PIDS_LIMIT:
            raise ValueError(f"pids_limit must be at least {MIN_PIDS_LIMIT}")

        if self.cpu_rate is not None:
            if isinstance(self.cpu_rate, bool) or not isinstance(
                self.cpu_rate,
                (int, float),
            ):
                raise TypeError("cpu_rate must be a number or None")
            if self.cpu_rate <= 0:
                raise ValueError("cpu_rate must be greater than zero")

        if self.network_mode not in NETWORK_MODES:
            raise ValueError(f"unsupported network mode: {self.network_mode!r}")
        for name, value in (
            ("read_only_root", self.read_only_root),
            ("drop_all_capabilities", self.drop_all_capabilities),
            ("no_new_privileges", self.no_new_privileges),
        ):
            _require_bool(value, name)
        if not self.read_only_root:
            raise ValueError("the sandbox always uses a read-only root filesystem")
        if not self.drop_all_capabilities:
            raise ValueError("the sandbox always drops every capability")
        if not self.no_new_privileges:
            raise ValueError("the sandbox always sets no-new-privileges")

        _require_identifier(self.seccomp_profile, "seccomp_profile")
        if self.seccomp_profile == "unconfined":
            raise ValueError("seccomp must never be unconfined")

        if not isinstance(self.tmpfs, tuple):
            raise TypeError("tmpfs must be a tuple")
        if len(self.tmpfs) > MAX_TMPFS:
            raise ValueError("too many tmpfs mounts")
        targets = [str(entry.target) for entry in self.tmpfs]
        for entry in self.tmpfs:
            if not isinstance(entry, ContainerTmpfs):
                raise TypeError("tmpfs must contain ContainerTmpfs values")
        if len(targets) != len(set(targets)):
            raise ValueError("tmpfs targets must not repeat")


@dataclass(frozen=True, slots=True)
class ContainerCreatePlan:
    runtime: ContainerRuntimeName
    name: str
    image: ContainerImage
    labels: ContainerLabels
    mounts: tuple[ContainerMount, ...]
    security: ContainerSecurityProfile
    workdir: PurePosixPath
    argv: tuple[str, ...]
    env_file: Path | None = None
    plan_version: str = PLAN_VERSION

    def __post_init__(self) -> None:
        if self.runtime != "docker":
            raise ValueError(
                f"runtime dialect is not implemented: {self.runtime!r}"
            )
        _require_identifier(self.name, "container name")
        if len(self.name) > MAX_NAME_LENGTH:
            raise ValueError("container name is too long")
        if not _NAME_PATTERN.match(self.name):
            raise ValueError("container name contains unsupported characters")

        if not isinstance(self.image, ContainerImage):
            raise TypeError("image must be a ContainerImage")
        if not isinstance(self.labels, ContainerLabels):
            raise TypeError("labels must be a ContainerLabels")
        if self.labels.image_digest != self.image.digest:
            raise ValueError("labels must record the exact image digest")
        if not isinstance(self.security, ContainerSecurityProfile):
            raise TypeError("security must be a ContainerSecurityProfile")

        if not isinstance(self.mounts, tuple) or not self.mounts:
            raise ValueError("a plan requires at least the workspace mount")
        if len(self.mounts) > MAX_MOUNTS:
            raise ValueError("too many mounts")
        for mount in self.mounts:
            if not isinstance(mount, ContainerMount):
                raise TypeError("mounts must contain ContainerMount values")
        targets = [str(mount.target) for mount in self.mounts]
        if len(targets) != len(set(targets)):
            raise ValueError("mount targets must not repeat")
        if str(CONTAINER_WORKSPACE) not in targets:
            raise ValueError("a plan requires the canonical workspace mount")

        object.__setattr__(
            self,
            "workdir",
            _require_posix_target(self.workdir, "workdir"),
        )
        if not _is_within(self.workdir, CONTAINER_WORKSPACE):
            raise ValueError("workdir must live inside the workspace mount")

        if not isinstance(self.argv, tuple) or not self.argv:
            raise ValueError("argv must be a non-empty tuple")
        if len(self.argv) > MAX_ARGV:
            raise ValueError("argv is too long")
        for index, argument in enumerate(self.argv):
            _require_text(argument, f"argv[{index}]", maximum=MAX_ARGUMENT_BYTES)

        if self.env_file is not None:
            if not isinstance(self.env_file, Path):
                raise TypeError("env_file must be a pathlib.Path or None")
            if not self.env_file.is_absolute():
                raise ValueError("env_file must be absolute")

        _require_identifier(self.plan_version, "plan_version")

    @property
    def workspace_mount(self) -> ContainerMount:
        return next(
            mount
            for mount in self.mounts
            if mount.target == CONTAINER_WORKSPACE
        )


@dataclass(frozen=True, slots=True)
class ContainerInspection:
    container_id: str
    state: ContainerState
    labels: tuple[tuple[str, str], ...]
    image_digest: str
    exit_code: int | None = None
    oom_killed: bool = False
    error: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _require_identifier(self.container_id, "container_id")
        if not _CONTAINER_ID_PATTERN.match(self.container_id):
            raise ValueError("container_id must be a full immutable hex ID")
        if self.state not in CONTAINER_STATES:
            raise ValueError(f"unknown container state: {self.state!r}")
        if not isinstance(self.labels, tuple):
            raise TypeError("labels must be a tuple")
        for pair in self.labels:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise TypeError("labels must contain key/value pairs")
            _require_text(pair[0], "label key", maximum=MAX_LABEL_VALUE_BYTES)
            _require_text(pair[1], "label value", maximum=MAX_LABEL_VALUE_BYTES)
        _require_identifier(self.image_digest, "image_digest")
        if not _DIGEST_PATTERN.match(self.image_digest):
            raise ValueError("image_digest must be a full sha256 digest")

        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise TypeError("exit_code must be an integer or None")
        _require_bool(self.oom_killed, "oom_killed")
        if self.error is not None:
            _require_text(self.error, "error", maximum=MAX_DIAGNOSTIC_BYTES)
        if self.state == "created" and self.exit_code not in (None, 0):
            raise ValueError("a created container cannot report a nonzero exit")

    @property
    def running(self) -> bool:
        return self.state in {"running", "paused", "restarting"}

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_CONTAINER_STATES

    def label_map(self) -> dict[str, str]:
        return dict(self.labels)


@dataclass(frozen=True, slots=True)
class ContainerBackendFacts:
    runtime: ContainerRuntimeName
    runtime_version: str
    image: ContainerImage | None
    supports_read_only_root: bool = False
    supports_bind_mounts: bool = False
    supports_tmpfs: bool = False
    supports_capability_drop: bool = False
    supports_no_new_privileges: bool = False
    supports_none_network: bool = False
    supports_memory_limit: bool = False
    supports_pids_limit: bool = False
    cpu_enforcement: CapabilityLevel = "unsupported"
    dialect_implemented: bool = False
    daemon_reachable: bool = False
    platform_supported: bool = False

    def __post_init__(self) -> None:
        _require_identifier(self.runtime, "runtime")
        _require_text(self.runtime_version, "runtime_version", maximum=MAX_IDENTIFIER_BYTES)
        if self.image is not None and not isinstance(self.image, ContainerImage):
            raise TypeError("image must be a ContainerImage or None")

    @property
    def available(self) -> bool:
        return not self.unavailable_reasons()

    def unavailable_reasons(self) -> tuple[UnavailableReason, ...]:
        reasons: list[UnavailableReason] = []
        if not self.dialect_implemented:
            reasons.append(
                UnavailableReason(
                    code="runtime-dialect-not-implemented",
                    message=(
                        f"The {self.runtime} dialect is not implemented and "
                        "tested for sandbox execution."
                    ),
                )
            )
        if not self.daemon_reachable:
            reasons.append(
                UnavailableReason(
                    code="container-runtime-unreachable",
                    message="The container runtime service is unreachable.",
                )
            )
        if not self.platform_supported:
            reasons.append(
                UnavailableReason(
                    code="container-platform-unsupported",
                    message="This host platform has no certified sandbox adapter.",
                )
            )
        if self.image is None:
            reasons.append(
                UnavailableReason(
                    code="sandbox-image-missing",
                    message=(
                        "The pinned execution image is not present locally and "
                        "launch never pulls."
                    ),
                )
            )
        missing = tuple(
            name
            for name, supported in (
                ("read-only-root", self.supports_read_only_root),
                ("bind-mounts", self.supports_bind_mounts),
                ("tmpfs", self.supports_tmpfs),
                ("capability-drop", self.supports_capability_drop),
                ("no-new-privileges", self.supports_no_new_privileges),
                ("none-network", self.supports_none_network),
                ("memory-limit", self.supports_memory_limit),
                ("pids-limit", self.supports_pids_limit),
            )
            if not supported
        )
        if missing:
            reasons.append(
                UnavailableReason(
                    code="container-security-option-unsupported",
                    message=(
                        "The runtime did not verify required sandbox options: "
                        + ", ".join(missing)
                    ),
                )
            )
        return tuple(reasons)

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            filesystem_isolation="enforced",
            network_isolation="enforced",
            memory_limits="enforced",
            cpu_limits=self.cpu_enforcement,
            process_limits="enforced",
            timeout_enforcement="enforced",
            cancellation="enforced",
            supported_execution_modes=("exec", "shell"),
            supported_filesystem_modes=("workspace-read", "workspace-write"),
            supported_shells=("posix",),
        )


def _is_within(candidate: PurePosixPath, root: PurePosixPath) -> bool:
    return candidate == root or root in candidate.parents
