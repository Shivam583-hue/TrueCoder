from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

from truecoder.execution.audit.models import AuditRunHandle
from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.errors import InvalidExecutionStateError
from truecoder.execution.models import ExecutionContext


class CancellationOutcome(str, Enum):
    REQUESTED = "requested"
    ALREADY_REQUESTED = "already_requested"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class ActiveExecution:
    context: ExecutionContext
    cancellation_source: CancellationSource

    audit_handle: AuditRunHandle | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.context, ExecutionContext):
            raise TypeError("context must be an ExecutionContext")
        if not isinstance(self.cancellation_source, CancellationSource):
            raise TypeError("cancellation_source must be a CancellationSource")
        if self.audit_handle is not None and not isinstance(
            self.audit_handle,
            AuditRunHandle,
        ):
            raise TypeError("audit_handle must be an AuditRunHandle or None")


class ExecutionRegistry:
    """Track active execution controls by their opaque execution IDs."""

    def __init__(self) -> None:
        self._entries: dict[str, ActiveExecution] = {}
        self._lock = asyncio.Lock()

    async def register(self, entry: ActiveExecution) -> None:
        if not isinstance(entry, ActiveExecution):
            raise TypeError("entry must be an ActiveExecution")

        execution_id = entry.context.execution_id
        async with self._lock:
            if execution_id in self._entries:
                raise InvalidExecutionStateError(
                    f"execution is already active: {execution_id}",
                    execution_id=execution_id,
                    operation="register",
                )
            self._entries[execution_id] = entry

    async def get(self, execution_id: str) -> ActiveExecution | None:
        execution_id = self._validate_execution_id(execution_id)
        async with self._lock:
            return self._entries.get(execution_id)

    async def request_cancellation(
        self,
        execution_id: str,
        *,
        reason: str,
    ) -> CancellationOutcome:
        execution_id = self._validate_execution_id(execution_id)
        async with self._lock:
            entry = self._entries.get(execution_id)
            if entry is None:
                return CancellationOutcome.NOT_FOUND
            requested = entry.cancellation_source.cancel(reason)

        return (
            CancellationOutcome.REQUESTED
            if requested
            else CancellationOutcome.ALREADY_REQUESTED
        )

    async def unregister(
        self,
        execution_id: str,
        *,
        expected: ActiveExecution,
    ) -> bool:
        execution_id = self._validate_execution_id(execution_id)
        if not isinstance(expected, ActiveExecution):
            raise TypeError("expected must be an ActiveExecution")

        async with self._lock:
            current = self._entries.get(execution_id)
            if current is not expected:
                return False
            del self._entries[execution_id]
            return True

    async def active_execution_ids(self) -> tuple[str, ...]:
        async with self._lock:
            return tuple(self._entries)

    @staticmethod
    def _validate_execution_id(execution_id: object) -> str:
        if not isinstance(execution_id, str):
            raise TypeError("execution_id must be a string")
        normalized = execution_id.strip()
        if not normalized:
            raise ValueError("execution_id cannot be empty")
        return normalized
