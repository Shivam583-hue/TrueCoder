from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from truecoder.execution.environment import (
    ConstructedEnvironment,
    EnvironmentPolicy,
    construct_environment,
)
from truecoder.execution.models import (
    BackendName,
    ExecutionPlatform,
    ExecutionRequest,
    PolicyDecision,
    ResolvedShellKind,
)

if TYPE_CHECKING:
    from truecoder.execution.backends.models import BackendDescriptor, SelectedBackend

_PLATFORM_BY_BACKEND: Final[dict[BackendName, ExecutionPlatform]] = {
    "posix": "posix",
    "windows": "windows",
    "container": "posix",
}


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    request: ExecutionRequest
    backend: BackendDescriptor
    environment: ConstructedEnvironment
    resolved_shell: ResolvedShellKind | None


def prepare_execution(
    original_request: ExecutionRequest,
    decision: PolicyDecision,
    selection: SelectedBackend,
    *,
    host_environment: Mapping[str, str],
    environment_policy: EnvironmentPolicy,
) -> PreparedExecution:

    effective_request = replace(
        original_request,
        limits=decision.effective_limits,
    )
    environment = construct_environment(
        platform=_PLATFORM_BY_BACKEND[selection.descriptor.name],
        inherited=host_environment,
        requested=effective_request.environment,
        policy=environment_policy,
    )
    return PreparedExecution(
        request=effective_request,
        backend=selection.descriptor,
        environment=environment,
        resolved_shell=selection.resolved_shell,
    )
