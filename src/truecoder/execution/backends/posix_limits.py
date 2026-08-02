from __future__ import annotations

import math
import resource
from dataclasses import dataclass

from ..models import ExecutionLimits


@dataclass(frozen=True, slots=True)
class RlimitSetting:
    name: str
    resource_id: int
    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("rlimit name must not be empty")
        if isinstance(self.resource_id, bool) or not isinstance(
            self.resource_id,
            int,
        ):
            raise TypeError("rlimit resource_id must be an integer")
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError("rlimit value must be an integer")
        if self.value <= 0:
            raise ValueError("rlimit value must be positive")


@dataclass(frozen=True, slots=True)
class AppliedRlimit:
    name: str
    requested: int
    soft: int
    hard: int


def build_rlimit_settings(
    limits: ExecutionLimits,
) -> tuple[RlimitSetting, ...]:
    if not isinstance(limits, ExecutionLimits):
        raise TypeError("limits must be ExecutionLimits")
    settings: list[RlimitSetting] = []
    if limits.memory_bytes is not None and hasattr(resource, "RLIMIT_AS"):
        settings.append(
            RlimitSetting(
                name="memory",
                resource_id=resource.RLIMIT_AS,
                value=limits.memory_bytes,
            )
        )
    if limits.cpu_seconds is not None and hasattr(resource, "RLIMIT_CPU"):
        settings.append(
            RlimitSetting(
                name="cpu",
                resource_id=resource.RLIMIT_CPU,
                value=max(1, math.ceil(limits.cpu_seconds)),
            )
        )
    if limits.max_processes is not None and hasattr(resource, "RLIMIT_NPROC"):
        settings.append(
            RlimitSetting(
                name="processes",
                resource_id=resource.RLIMIT_NPROC,
                value=limits.max_processes,
            )
        )
    return tuple(settings)


def apply_rlimit_settings(
    settings: tuple[RlimitSetting, ...],
) -> tuple[AppliedRlimit, ...]:
    if not isinstance(settings, tuple):
        raise TypeError("settings must be a tuple")
    applied: list[AppliedRlimit] = []
    for setting in settings:
        if not isinstance(setting, RlimitSetting):
            raise TypeError("settings must contain RlimitSetting values")
        current_soft, current_hard = resource.getrlimit(setting.resource_id)
        soft = _stricter_limit(setting.value, current_soft)
        hard = _stricter_limit(setting.value, current_hard)
        if hard != resource.RLIM_INFINITY:
            soft = min(soft, hard)
        resource.setrlimit(setting.resource_id, (soft, hard))
        applied.append(
            AppliedRlimit(
                name=setting.name,
                requested=setting.value,
                soft=soft,
                hard=hard,
            )
        )
    return tuple(applied)


def _stricter_limit(requested: int, current: int) -> int:
    if current == resource.RLIM_INFINITY:
        return requested
    return min(requested, current)
