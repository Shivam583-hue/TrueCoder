from __future__ import annotations

from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.models import ExecutionContext
from truecoder.execution.registry import (
    ActiveExecution,
    CancellationOutcome,
    ExecutionRegistry,
)


class ExecutionService:
    """Own active execution registration and cancellation routing."""

    def __init__(self, registry: ExecutionRegistry | None = None) -> None:
        self._registry = registry or ExecutionRegistry()

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
        return await self._registry.request_cancellation(
            execution_id,
            reason=reason,
        )

    async def unregister(self, entry: ActiveExecution) -> bool:
        return await self._registry.unregister(
            entry.context.execution_id,
            expected=entry,
        )
