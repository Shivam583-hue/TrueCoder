from dataclasses import dataclass
from os import replace
from typing import Mapping

from truecoder.execution.backends.models import BackendDescriptor, SelectedBackend
from truecoder.execution.environment import (
    ConstructedEnvironment,
    EnvironmentPolicy,
    construct_environment,
)
from truecoder.execution.models import (
    ExecutionRequest,
    PolicyDecision,
    ResolvedShellKind,
)


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
        effective_request,
        host_environment=host_environment,
        policy=environment_policy,
    )
    return PreparedExecution(
        request=effective_request,
        backend=selection.descriptor,
        environment=environment,
        resolved_shell=selection.resolved_shell,
    )
