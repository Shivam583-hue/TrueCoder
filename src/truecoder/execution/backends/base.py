from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
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
        context: ExecutionContext,
        cancellation: CancellationToken,
        register_resource: BackendResourceRegistrar,
    ) -> ExecutionHandle: ...
