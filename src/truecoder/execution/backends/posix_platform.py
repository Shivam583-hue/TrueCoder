from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..models import CapabilityLevel

PosixPlatform = Final
POSIX_PLATFORMS: Final = frozenset({"linux", "macos"})

_PER_USER_PROCESS_LIMIT_PLATFORMS: Final = frozenset({"macos"})
_CGROUP_PLATFORMS: Final = frozenset({"linux"})

_DEFAULT_SHELL_SEARCH: Final[dict[str, tuple[str, ...]]] = {
    "linux": ("/bin/sh", "/bin/bash", "/usr/bin/sh", "/usr/bin/bash"),
    "macos": ("/bin/sh", "/bin/bash", "/bin/zsh", "/usr/bin/env"),
}


@dataclass(frozen=True, slots=True)
class PosixPlatformProfile:
    system: str
    supports_cgroups: bool
    process_limit_is_per_user: bool
    boot_identity_available: bool
    process_start_ticks_available: bool
    shell_search_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.system not in POSIX_PLATFORMS:
            raise ValueError(f"unsupported POSIX platform: {self.system!r}")

    @property
    def applies_process_rlimit(self) -> bool:
        return not self.process_limit_is_per_user

    @property
    def process_limit_level(self) -> CapabilityLevel:
        if self.supports_cgroups:
            return "best_effort"
        if self.process_limit_is_per_user:
            return "unsupported"
        return "best_effort"

    @property
    def memory_limit_level(self) -> CapabilityLevel:
        return "best_effort"

    @property
    def cpu_limit_level(self) -> CapabilityLevel:
        return "best_effort"

    @property
    def can_prove_resource_ownership(self) -> bool:
        return self.boot_identity_available and self.process_start_ticks_available

    def unsupported_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.supports_cgroups:
            reasons.append("cgroup-controllers-unavailable")
        if self.process_limit_is_per_user:
            reasons.append("process-limit-is-per-user")
        if not self.can_prove_resource_ownership:
            reasons.append("resource-ownership-unprovable-after-restart")
        return tuple(reasons)


def profile_for(system: str) -> PosixPlatformProfile:
    if system not in POSIX_PLATFORMS:
        raise ValueError(f"unsupported POSIX platform: {system!r}")
    return PosixPlatformProfile(
        system=system,
        supports_cgroups=system in _CGROUP_PLATFORMS,
        process_limit_is_per_user=system in _PER_USER_PROCESS_LIMIT_PLATFORMS,
        boot_identity_available=system == "linux",
        process_start_ticks_available=system == "linux",
        shell_search_paths=_DEFAULT_SHELL_SEARCH[system],
    )
