from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeAlias, runtime_checkable

from truecoder.execution.preparation import PreparedExecution

from ..audit.models import BackendResourceIdentifier
from ..cancellation import CancellationToken
from ..models import (
    ExecutionContext,
    ExecutionRequest,
    TerminationReason,
)
from .models import (
    BackendDescriptor,
    BackendExit,
    BackendOutputChunk,
    CleanupResult,
)

BackendResourceRegistrar: TypeAlias = Callable[
    [BackendResourceIdentifier],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class BackendStartContext:
    execution: ExecutionContext
    audit_run_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution, ExecutionContext):
            raise TypeError("execution must be an ExecutionContext")
        if not isinstance(self.audit_run_id, str):
            raise TypeError("audit_run_id must be a string")
        if not self.audit_run_id.strip():
            raise ValueError("audit_run_id cannot be empty")

    @property
    def execution_id(self) -> str:
        return self.execution.execution_id


@runtime_checkable
class ExecutionHandle(Protocol):
    @property
    def execution_id(self) -> str: ...

    @property
    def resource(self) -> BackendResourceIdentifier: ...

    def output(self) -> AsyncIterator[BackendOutputChunk]: ...

    async def wait(self) -> BackendExit: ...

    async def terminate(
        self,
        reason: TerminationReason,
        grace_seconds: float,
    ) -> None: ...

    async def cleanup(self) -> CleanupResult: ...


@runtime_checkable
class ExecutionBackend(Protocol):
    @property
    def descriptor(self) -> BackendDescriptor: ...

    async def start(
        self,
        prepared: PreparedExecution,
        request: ExecutionRequest,
        context: BackendStartContext,
        cancellation: CancellationToken,
        register_resource: BackendResourceRegistrar,
    ) -> ExecutionHandle: ...
