from __future__ import annotations

from typing import Final

from .backends.models import (
    BackendCompatibility,
    BackendDescriptor,
    DiscoverySnapshot,
    SelectedBackend,
    UnavailableReason,
)
from .errors import BackendSelectionError, NoCompatibleBackendError
from .models import (
    CAPABILITY_LEVELS,
    CAPABILITY_REQUIREMENT_LEVELS,
    BackendName,
    CapabilityLevel,
    CapabilityRequirementLevel,
    ExecutionRequest,
    PolicyDecision,
    ResolvedShellKind,
)

_CAPABILITY_RANK: Final = {
    "unsupported": 0,
    "best_effort": 1,
    "enforced": 2,
}

_REQUIREMENT_RANK: Final = {
    "none": 0,
    "best_effort": 1,
    "enforced": 2,
}

_CAPABILITY_FIELDS: Final = (
    ("filesystem_isolation", "filesystem-isolation"),
    ("network_isolation", "network-isolation"),
    ("memory_limits", "memory-limits"),
    ("cpu_limits", "cpu-limits"),
    ("process_limits", "process-limits"),
    ("timeout_enforcement", "timeout-enforcement"),
    ("cancellation", "cancellation"),
)


def capability_meets(
    actual: CapabilityLevel,
    required: CapabilityRequirementLevel,
) -> bool:
    if actual not in CAPABILITY_LEVELS:
        raise ValueError(f"unknown capability level: {actual!r}")
    if required not in CAPABILITY_REQUIREMENT_LEVELS:
        raise ValueError(f"unknown capability requirement: {required!r}")
    return _CAPABILITY_RANK[actual] >= _REQUIREMENT_RANK[required]


def check_backend(
    descriptor: BackendDescriptor,
    request: ExecutionRequest,
    decision: PolicyDecision,
) -> BackendCompatibility:
    """Compare one discovered backend against one unchanged security contract."""

    if not isinstance(descriptor, BackendDescriptor):
        raise TypeError("descriptor must be a BackendDescriptor")
    if not isinstance(request, ExecutionRequest):
        raise TypeError("request must be an ExecutionRequest")
    if not isinstance(decision, PolicyDecision):
        raise TypeError("decision must be a PolicyDecision")
    if not decision.allowed:
        raise BackendSelectionError(
            "cannot select a backend for a policy-denied execution",
            operation="check_backend",
        )
    if not descriptor.available:
        return BackendCompatibility(
            backend=descriptor.name,
            compatible=False,
            reasons=descriptor.unavailable_reasons,
        )

    reasons: list[UnavailableReason] = []
    capabilities = descriptor.capabilities
    if request.mode not in capabilities.supported_execution_modes:
        reasons.append(
            _reason(
                "execution-mode-unsupported",
                f"Backend {descriptor.name!r} does not support "
                f"execution mode {request.mode!r}.",
            )
        )
    if request.filesystem_mode not in capabilities.supported_filesystem_modes:
        reasons.append(
            _reason(
                "filesystem-mode-unsupported",
                f"Backend {descriptor.name!r} does not support "
                f"filesystem mode {request.filesystem_mode!r}.",
            )
        )

    resolved_shell = _resolve_shell(descriptor, request, reasons)
    requirements = decision.requirements
    for field_name, reason_code in _CAPABILITY_FIELDS:
        actual = getattr(capabilities, field_name)
        required = getattr(requirements, field_name)
        if capability_meets(actual, required):
            continue
        reasons.append(
            _reason(
                f"{reason_code}-insufficient",
                f"Backend {descriptor.name!r} provides {actual!r} "
                f"{field_name.replace('_', ' ')}, but policy requires "
                f"{required!r}.",
            )
        )

    if reasons:
        return BackendCompatibility(
            backend=descriptor.name,
            compatible=False,
            reasons=tuple(reasons),
        )
    return BackendCompatibility(
        backend=descriptor.name,
        compatible=True,
        resolved_shell=resolved_shell,
    )


def select_backend(
    request: ExecutionRequest,
    decision: PolicyDecision,
    snapshot: DiscoverySnapshot,
) -> SelectedBackend:
    """Select deterministically without changing any request or requirement."""

    if not isinstance(request, ExecutionRequest):
        raise TypeError("request must be an ExecutionRequest")
    if not isinstance(decision, PolicyDecision):
        raise TypeError("decision must be a PolicyDecision")
    if not isinstance(snapshot, DiscoverySnapshot):
        raise TypeError("snapshot must be a DiscoverySnapshot")
    if not decision.allowed:
        raise BackendSelectionError(
            "cannot select a backend for a policy-denied execution",
            operation="select_backend",
        )

    candidate_names = _candidate_names(request, snapshot)
    failures: list[tuple[BackendName, tuple[str, ...]]] = []
    for name in candidate_names:
        descriptor = snapshot.backend(name)
        compatibility = check_backend(descriptor, request, decision)
        if compatibility.compatible:
            return SelectedBackend(
                descriptor=descriptor,
                resolved_shell=compatibility.resolved_shell,
                selection_reason=_selection_reason(
                    request,
                    descriptor,
                    failures,
                ),
            )
        failures.append(
            (
                name,
                tuple(reason.message for reason in compatibility.reasons),
            )
        )

    raise NoCompatibleBackendError(
        failures=tuple(failures),
        preference=request.backend,
    )


def _candidate_names(
    request: ExecutionRequest,
    snapshot: DiscoverySnapshot,
) -> tuple[BackendName, ...]:
    if request.backend == "container":
        return ("container",)
    if request.backend == "local":
        if snapshot.host.family == "posix":
            return ("posix",)
        if snapshot.host.family == "windows":
            return ("windows",)
        return ("posix", "windows")
    if snapshot.host.family == "posix":
        return ("posix", "container")
    if snapshot.host.family == "windows":
        return ("windows", "container")
    return ("posix", "windows", "container")


def _resolve_shell(
    descriptor: BackendDescriptor,
    request: ExecutionRequest,
    reasons: list[UnavailableReason],
) -> ResolvedShellKind | None:
    if request.mode != "shell":
        return None
    supported = descriptor.capabilities.supported_shells
    if request.shell_kind != "auto":
        if request.shell_kind in supported:
            return request.shell_kind
        reasons.append(
            _reason(
                "shell-unsupported",
                f"Backend {descriptor.name!r} does not provide the explicitly "
                f"requested shell {request.shell_kind!r}.",
            )
        )
        return None
    preferred: tuple[ResolvedShellKind, ...] = (
        ("powershell", "posix")
        if descriptor.name == "windows"
        else ("posix", "powershell")
    )
    resolved = next((shell for shell in preferred if shell in supported), None)
    if resolved is None:
        reasons.append(
            _reason(
                "shell-unavailable",
                f"Backend {descriptor.name!r} has no supported shell.",
            )
        )
    return resolved


def _selection_reason(
    request: ExecutionRequest,
    descriptor: BackendDescriptor,
    failures: list[tuple[BackendName, tuple[str, ...]]],
) -> str:
    if request.backend != "auto":
        return (
            f"Selected the compatible backend required by "
            f"{request.backend!r} preference."
        )
    if failures:
        rejected = ", ".join(name for name, _reasons in failures)
        return (
            f"Selected compatible backend {descriptor.name!r} after rejecting "
            f"incompatible candidate(s): {rejected}."
        )
    return f"Selected first compatible automatic backend {descriptor.name!r}."


def _reason(code: str, message: str) -> UnavailableReason:
    return UnavailableReason(code=code, message=message)
