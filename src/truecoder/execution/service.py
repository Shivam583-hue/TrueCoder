from __future__ import annotations

from truecoder.execution.audit.models import AuditEventType
from truecoder.execution.audit.service import AuditService
from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.models import (
    ExecutionContext,
    ExecutionResult,
    PolicyDecision,
)
from truecoder.execution.preparation import PreparedExecution
from truecoder.execution.registry import (
    ActiveExecution,
    CancellationOutcome,
    ExecutionRegistry,
)
from truecoder.execution.runner import ExecutionRunner


class ExecutionService:
    def __init__(
        self,
        registry: ExecutionRegistry | None = None,
        *,
        runner: ExecutionRunner | None = None,
        audit: AuditService | None = None,
    ) -> None:
        if runner is not None and not isinstance(runner, ExecutionRunner):
            raise TypeError("runner must be an ExecutionRunner")
        if audit is not None and not isinstance(audit, AuditService):
            raise TypeError("audit must be an AuditService")

        self._registry = registry or ExecutionRegistry()
        self._runner = runner
        self._audit = audit

    @property
    def registry(self) -> ExecutionRegistry:
        return self._registry

    async def run(
        self,
        prepared: PreparedExecution,
        decision: PolicyDecision,
        context: ExecutionContext,
    ) -> ExecutionResult:
        if self._runner is None:
            raise RuntimeError("this service was built without an execution runner")
        return await self._runner.run(prepared, decision, context)

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
