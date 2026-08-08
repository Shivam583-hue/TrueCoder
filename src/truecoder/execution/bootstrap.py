from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from truecoder.execution.approval import ApprovalService, ExecutionApprovalGate
from truecoder.execution.audit import (
    AuditRecoveryCoordinator,
    AuditService,
    SQLiteAuditStore,
    TerminalOutcome,
    default_audit_database_path,
)
from truecoder.execution.audit.retention import RetentionPolicy
from truecoder.execution.backends.base import ExecutionBackend
from truecoder.execution.backends.container import ContainerBackend
from truecoder.execution.backends.container_plan import (
    DEFAULT_MEMORY_BYTES,
    DEFAULT_PIDS_LIMIT,
    ContainerLaunchConfig,
    load_image_lock,
)
from truecoder.execution.backends.container_recovery import ContainerRecoveryHandler
from truecoder.execution.backends.container_runtime import DockerRuntime
from truecoder.execution.backends.models import (
    BackendDescriptor,
    DiscoverySnapshot,
)
from truecoder.execution.backends.posix import PosixBackend
from truecoder.execution.backends.posix_identity import current_host_id
from truecoder.execution.backends.posix_recovery import PosixRecoveryHandler
from truecoder.execution.backends.registry import BackendRegistry
from truecoder.execution.backends.windows import (
    WindowsBackend,
    WindowsRecoveryHandler,
)
from truecoder.execution.backends.windows_native import WINDOWS
from truecoder.execution.defaults import DEFAULT_EXECUTION_LIMITS
from truecoder.execution.discovery import (
    DEFAULT_IMAGE_LOCK,
    DiscoveryIO,
    discover_execution_environment,
)
from truecoder.execution.environment import EnvironmentPolicy
from truecoder.execution.errors import ExecutionInfrastructureError
from truecoder.execution.events import ExecutionEventSink
from truecoder.execution.models import BackendName, RiskLevel
from truecoder.execution.policy import PolicyConfig
from truecoder.execution.registry import ExecutionRegistry
from truecoder.execution.runner import ExecutionRunner, PreviewSink
from truecoder.execution.service import ExecutionService
from truecoder.execution.trusted_rules import (
    TrustedRulesError,
    default_trusted_rules_path,
    load_trusted_rules,
)


def default_policy_config() -> PolicyConfig:
    return PolicyConfig(
        version="truecoder-execution-v1",
        limit_ceiling=DEFAULT_EXECUTION_LIMITS,
        minimum_isolation="enforced",
        limit_enforcement="enforced",
        unknown_risk=RiskLevel.MEDIUM,
    )


@dataclass(frozen=True, slots=True)
class ExecutionBootstrapConfig:
    enabled: bool = True
    audit_database_path: Path = field(default_factory=default_audit_database_path)
    image_lock_path: Path = DEFAULT_IMAGE_LOCK
    trusted_rules_path: Path = field(default_factory=default_trusted_rules_path)
    policy_config: PolicyConfig = field(default_factory=default_policy_config)
    environment_policy: EnvironmentPolicy = field(default_factory=EnvironmentPolicy)
    container_default_memory_bytes: int = DEFAULT_MEMORY_BYTES
    container_default_pids_limit: int = DEFAULT_PIDS_LIMIT
    container_cpu_rate_ceiling: float | None = None
    container_isolated_network: str | None = None
    retention_policy: RetentionPolicy = field(default_factory=RetentionPolicy)
    event_sink: ExecutionEventSink | None = None
    preview_sink: PreviewSink | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a boolean")
        if not isinstance(self.audit_database_path, Path):
            raise TypeError("audit_database_path must be a pathlib.Path")
        if not isinstance(self.image_lock_path, Path):
            raise TypeError("image_lock_path must be a pathlib.Path")
        if not isinstance(self.trusted_rules_path, Path):
            raise TypeError("trusted_rules_path must be a pathlib.Path")
        if not isinstance(self.policy_config, PolicyConfig):
            raise TypeError("policy_config must be a PolicyConfig")
        if not isinstance(self.environment_policy, EnvironmentPolicy):
            raise TypeError("environment_policy must be an EnvironmentPolicy")
        for name, value in (
            ("container_default_memory_bytes", self.container_default_memory_bytes),
            ("container_default_pids_limit", self.container_default_pids_limit),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.container_cpu_rate_ceiling is not None:
            value = self.container_cpu_rate_ceiling
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("container_cpu_rate_ceiling must be a number or None")
            if value <= 0:
                raise ValueError("container_cpu_rate_ceiling must be greater than zero")
        if self.container_isolated_network is not None:
            value = self.container_isolated_network
            if not isinstance(value, str):
                raise TypeError("container_isolated_network must be a string or None")
            if not value.strip():
                raise ValueError("container_isolated_network cannot be empty")
        if not isinstance(self.retention_policy, RetentionPolicy):
            raise TypeError("retention_policy must be a RetentionPolicy")
        if not self.retention_policy.keep_nonterminal:
            raise ValueError("retention_policy must preserve nonterminal runs")
        if self.event_sink is not None and not isinstance(
            self.event_sink,
            ExecutionEventSink,
        ):
            raise TypeError("event_sink must implement ExecutionEventSink")
        if self.preview_sink is not None and not hasattr(
            self.preview_sink,
            "publish_bounded",
        ):
            raise TypeError("preview_sink must provide publish_bounded")


@dataclass(frozen=True, slots=True)
class BackendHealth:
    name: BackendName
    discovered: bool
    registered: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionHealthReport:
    enabled: bool
    audit_ready: bool
    recovery_ready: bool
    backends: tuple[BackendHealth, ...]
    failure_code: str | None = None

    @property
    def shell_available(self) -> bool:
        return (
            self.enabled
            and self.audit_ready
            and self.recovery_ready
            and any(backend.registered for backend in self.backends)
        )


@dataclass(frozen=True, slots=True)
class ExecutionRuntime:
    service: ExecutionService | None
    audit: AuditService | None
    discovery: DiscoverySnapshot | None
    backends: tuple[ExecutionBackend, ...]
    health: ExecutionHealthReport

    @property
    def shell_available(self) -> bool:
        return self.service is not None and self.health.shell_available


async def bootstrap_execution(
    approval_service: ApprovalService,
    *,
    config: ExecutionBootstrapConfig | None = None,
    discovery_io: DiscoveryIO | None = None,
    discovery_snapshot: DiscoverySnapshot | None = None,
) -> ExecutionRuntime:
    if not isinstance(approval_service, ApprovalService):
        raise TypeError("approval_service must be an ApprovalService")
    settings = config or ExecutionBootstrapConfig()
    if not isinstance(settings, ExecutionBootstrapConfig):
        raise TypeError("config must be an ExecutionBootstrapConfig")

    try:
        audit = AuditService(SQLiteAuditStore(settings.audit_database_path))
    except (ExecutionInfrastructureError, OSError, TypeError, ValueError):
        return _unavailable_runtime(
            enabled=settings.enabled,
            failure_code="audit_unavailable",
        )

    try:
        trusted_rules = load_trusted_rules(settings.trusted_rules_path)
    except TrustedRulesError:
        return _unavailable_runtime(
            enabled=settings.enabled,
            audit=audit,
            failure_code="trusted_rules_invalid",
        )

    try:
        snapshot = discovery_snapshot or await discover_execution_environment(
            discovery_io,
            image_lock_path=settings.image_lock_path,
        )
    except (ExecutionInfrastructureError, OSError, TypeError, ValueError):
        return _unavailable_runtime(
            enabled=settings.enabled,
            audit=audit,
            failure_code="discovery_failed",
        )

    backends, build_failures = _build_backends(snapshot, settings)
    handlers = _recovery_handlers(snapshot)
    try:
        recovered = await AuditRecoveryCoordinator(
            audit,
            handlers,
        ).recover_startup(f"startup_{uuid.uuid4().hex}")
    except (ExecutionInfrastructureError, OSError, TypeError, ValueError):
        return _runtime_with_health(
            enabled=settings.enabled,
            audit=audit,
            snapshot=snapshot,
            backends=backends,
            build_failures=build_failures,
            recovery_ready=False,
            failure_code="recovery_unavailable",
        )

    recovery_failed = any(
        record.finalization is not None
        and record.finalization.outcome is TerminalOutcome.RECOVERY_FAILED
        for record in recovered
    )
    if recovery_failed:
        return _runtime_with_health(
            enabled=settings.enabled,
            audit=audit,
            snapshot=snapshot,
            backends=backends,
            build_failures=build_failures,
            recovery_ready=False,
            failure_code="recovery_failed",
        )

    try:
        await audit.apply_retention(settings.retention_policy)
    except (ExecutionInfrastructureError, OSError, TypeError, ValueError):
        return _runtime_with_health(
            enabled=settings.enabled,
            audit=audit,
            snapshot=snapshot,
            backends=backends,
            build_failures=build_failures,
            recovery_ready=True,
            failure_code="retention_failed",
        )

    if not settings.enabled or not backends:
        return _runtime_with_health(
            enabled=settings.enabled,
            audit=audit,
            snapshot=snapshot,
            backends=backends,
            build_failures=build_failures,
            recovery_ready=True,
            failure_code=(
                "execution_disabled" if not settings.enabled else "no_healthy_backend"
            ),
        )

    registry = ExecutionRegistry()
    runner = ExecutionRunner(
        audit,
        BackendRegistry(backends),
        registry=registry,
        approval_gate=ExecutionApprovalGate(
            approval_service,
            policy_version=settings.policy_config.version,
        ),
        event_sink=settings.event_sink,
        preview_sink=settings.preview_sink,
    )
    service = ExecutionService(
        registry,
        runner=runner,
        audit=audit,
        policy_config=settings.policy_config,
        discovery=snapshot,
        environment_policy=settings.environment_policy,
        host_environment=os.environ,
        trusted_rules=trusted_rules,
        container_network_configured=(settings.container_isolated_network is not None),
    )
    runtime = _runtime_with_health(
        enabled=True,
        audit=audit,
        snapshot=snapshot,
        backends=backends,
        build_failures=build_failures,
        recovery_ready=True,
        failure_code=None,
    )
    return ExecutionRuntime(
        service=service,
        audit=runtime.audit,
        discovery=runtime.discovery,
        backends=runtime.backends,
        health=runtime.health,
    )


def _build_backends(
    snapshot: DiscoverySnapshot,
    config: ExecutionBootstrapConfig,
) -> tuple[tuple[ExecutionBackend, ...], dict[BackendName, str]]:
    built: list[ExecutionBackend] = []
    failures: dict[BackendName, str] = {}

    posix = snapshot.backend("posix")
    if posix.available:
        try:
            built.append(
                PosixBackend.from_snapshot(
                    snapshot,
                    inherited_environment=os.environ,
                    environment_policy=config.environment_policy,
                )
            )
        except (ExecutionInfrastructureError, OSError, TypeError, ValueError):
            failures["posix"] = "backend construction failed"

    windows = snapshot.backend("windows")
    if windows.available:
        if not WINDOWS:
            failures["windows"] = "backend requires a windows host"
        else:
            try:
                built.append(WindowsBackend.from_snapshot(snapshot))
            except (ExecutionInfrastructureError, OSError, TypeError, ValueError):
                failures["windows"] = "backend construction failed"

    container = snapshot.backend("container")
    if container.available:
        try:
            if container.runtime is None:
                raise ValueError("container runtime descriptor is missing")
            image = load_image_lock(config.image_lock_path)
            runtime = DockerRuntime(container.runtime)
            built.append(
                ContainerBackend(
                    container,
                    runtime,
                    ContainerLaunchConfig(
                        image=image,
                        default_memory_bytes=config.container_default_memory_bytes,
                        default_pids_limit=config.container_default_pids_limit,
                        cpu_rate_ceiling=config.container_cpu_rate_ceiling,
                        isolated_network=config.container_isolated_network,
                    ),
                    host_id=current_host_id(),
                )
            )
        except (ExecutionInfrastructureError, OSError, TypeError, ValueError):
            failures["container"] = "backend construction failed"

    return tuple(built), failures


def _recovery_handlers(snapshot: DiscoverySnapshot):
    handlers = {}
    if snapshot.host.family == "posix":
        handlers["posix"] = PosixRecoveryHandler()
    if snapshot.host.system == "windows" and WINDOWS:
        handlers["windows"] = WindowsRecoveryHandler()
    docker = next(
        (
            runtime
            for runtime in snapshot.runtimes
            if runtime.name == "docker"
            and runtime.daemon_reachable
            and runtime.diagnostic is None
        ),
        None,
    )
    if docker is not None:
        handlers["container"] = ContainerRecoveryHandler(
            DockerRuntime(docker),
            host_id=current_host_id(),
        )
    return handlers


def _runtime_with_health(
    *,
    enabled: bool,
    audit: AuditService,
    snapshot: DiscoverySnapshot,
    backends: tuple[ExecutionBackend, ...],
    build_failures: dict[BackendName, str],
    recovery_ready: bool,
    failure_code: str | None,
) -> ExecutionRuntime:
    registered = {backend.descriptor.name for backend in backends}
    health = tuple(
        BackendHealth(
            name=descriptor.name,
            discovered=descriptor.available,
            registered=descriptor.name in registered,
            reasons=_health_reasons(descriptor, build_failures),
        )
        for descriptor in snapshot.backends
    )
    return ExecutionRuntime(
        service=None,
        audit=audit,
        discovery=snapshot,
        backends=backends,
        health=ExecutionHealthReport(
            enabled=enabled,
            audit_ready=True,
            recovery_ready=recovery_ready,
            backends=health,
            failure_code=failure_code,
        ),
    )


def _health_reasons(
    descriptor: BackendDescriptor,
    build_failures: dict[BackendName, str],
) -> tuple[str, ...]:
    failure = build_failures.get(descriptor.name)
    if failure is not None:
        return (failure,)
    return tuple(reason.message for reason in descriptor.unavailable_reasons)


def _unavailable_runtime(
    *,
    enabled: bool,
    audit: AuditService | None = None,
    failure_code: str,
) -> ExecutionRuntime:
    return ExecutionRuntime(
        service=None,
        audit=audit,
        discovery=None,
        backends=(),
        health=ExecutionHealthReport(
            enabled=enabled,
            audit_ready=audit is not None,
            recovery_ready=False,
            backends=(),
            failure_code=failure_code,
        ),
    )
