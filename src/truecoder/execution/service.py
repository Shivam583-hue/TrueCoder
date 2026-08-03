from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypeAlias

from truecoder.execution.audit.models import AuditEventType
from truecoder.execution.audit.service import AuditService
from truecoder.execution.backends.models import DiscoverySnapshot
from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.environment import EnvironmentPolicy
from truecoder.execution.errors import BackendSelectionError
from truecoder.execution.models import (
    ExecutionContext,
    ExecutionRequest,
    ExecutionResult,
    PolicyDecision,
)
from truecoder.execution.policy import PolicyConfig, evaluate_policy
from truecoder.execution.preparation import PreparedExecution, prepare_execution
from truecoder.execution.registry import (
    ActiveExecution,
    CancellationOutcome,
    ExecutionRegistry,
)
from truecoder.execution.runner import ExecutionRunner
from truecoder.execution.selection import select_backend

DiscoveryProvider: TypeAlias = Callable[[], DiscoverySnapshot]


class ExecutionService:
    def __init__(
        self,
        registry: ExecutionRegistry | None = None,
        *,
        runner: ExecutionRunner | None = None,
        audit: AuditService | None = None,
        policy_config: PolicyConfig | None = None,
        discovery: DiscoverySnapshot | DiscoveryProvider | None = None,
        environment_policy: EnvironmentPolicy | None = None,
        host_environment: Mapping[str, str] | None = None,
    ) -> None:
        if runner is not None and not isinstance(runner, ExecutionRunner):
            raise TypeError("runner must be an ExecutionRunner")
        if audit is not None and not isinstance(audit, AuditService):
            raise TypeError("audit must be an AuditService")
        if policy_config is not None and not isinstance(policy_config, PolicyConfig):
            raise TypeError("policy_config must be a PolicyConfig")
        if (
            discovery is not None
            and not isinstance(discovery, DiscoverySnapshot)
            and not callable(discovery)
        ):
            raise TypeError(
                "discovery must be a DiscoverySnapshot or a callable provider"
            )
        if environment_policy is not None and not isinstance(
            environment_policy,
            EnvironmentPolicy,
        ):
            raise TypeError("environment_policy must be an EnvironmentPolicy")
        if host_environment is not None and not isinstance(host_environment, Mapping):
            raise TypeError("host_environment must be a mapping")

        self._registry = registry or ExecutionRegistry()
        self._runner = runner
        self._audit = audit
        self._policy_config = policy_config
        self._discovery = discovery
        self._environment_policy = environment_policy or EnvironmentPolicy()
        self._host_environment = (
            dict(host_environment) if host_environment is not None else {}
        )

    @property
    def registry(self) -> ExecutionRegistry:
        return self._registry

    async def execute(
        self,
        request: ExecutionRequest,
        context: ExecutionContext,
    ) -> ExecutionResult:
        if not isinstance(request, ExecutionRequest):
            raise TypeError("request must be an ExecutionRequest")
        if not isinstance(context, ExecutionContext):
            raise TypeError("context must be an ExecutionContext")

        runner = self._require_runner()
        if self._policy_config is None:
            raise RuntimeError("this service was built without a policy configuration")

        decision = evaluate_policy(request, self._policy_config)
        if not decision.allowed:
            return await runner.deny(request, decision, context)

        try:
            selection = select_backend(request, decision, self._snapshot())
        except BackendSelectionError as error:
            return await runner.refuse(
                request,
                context,
                detail="backend_unavailable",
                error=error,
            )

        prepared = prepare_execution(
            request,
            decision,
            selection,
            host_environment=self._host_environment,
            environment_policy=self._environment_policy,
        )
        return await runner.run_prepared(prepared, decision, context)

    async def run_prepared(
        self,
        prepared: PreparedExecution,
        decision: PolicyDecision,
        context: ExecutionContext,
    ) -> ExecutionResult:
        return await self._require_runner().run_prepared(prepared, decision, context)

    async def register(self, context: ExecutionContext) -> ActiveExecution:
        entry = ActiveExecution(
            context=context,
            cancellation_source=CancellationSource(),
        )
        await self._registry.register(entry)
        return entry

    async def lookup(self, execution_id: str) -> ActiveExecution | None:
        return await self._registry.get(execution_id)

    async def cancel(
        self,
        execution_id: str,
        *,
        reason: str = "user",
    ) -> CancellationOutcome:
        outcome = await self._registry.request_cancellation(
            execution_id,
            reason=reason,
        )
        if outcome is CancellationOutcome.REQUESTED:
            await self._record_cancellation_requested(execution_id, reason)
        return outcome

    async def unregister(self, entry: ActiveExecution) -> bool:
        return await self._registry.unregister(
            entry.context.execution_id,
            expected=entry,
        )

    def _require_runner(self) -> ExecutionRunner:
        if self._runner is None:
            raise RuntimeError("this service was built without an execution runner")
        return self._runner

    def _snapshot(self) -> DiscoverySnapshot:
        if self._discovery is None:
            raise RuntimeError("this service was built without backend discovery")
        if isinstance(self._discovery, DiscoverySnapshot):
            return self._discovery

        snapshot = self._discovery()
        if not isinstance(snapshot, DiscoverySnapshot):
            raise TypeError("the discovery provider must return a DiscoverySnapshot")
        return snapshot

    async def _record_cancellation_requested(
        self,
        execution_id: str,
        reason: str,
    ) -> None:
        if self._audit is None:
            return

        entry = await self._registry.get(execution_id)
        if entry is None or entry.audit_handle is None:
            return

        try:
            await self._audit.append_event(
                entry.audit_handle,
                AuditEventType.CANCELLATION_REQUESTED,
                message=reason,
            )
        except Exception:  # noqa: BLE001
            return
