from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

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


@runtime_checkable
class ExecutionHandle(Protocol):
    """Own one exact backend resource from successful start through cleanup.

    The backend owns all partially acquired resources until ``start`` returns.
    A successful return transfers complete ownership to this handle. The
    execution service then owns the handle lifecycle and must eventually call
    ``cleanup``. Wait, terminate, and cleanup must be idempotent.
    """

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
    """Start executions only after discovery and capability selection."""

    @property
    def descriptor(self) -> BackendDescriptor: ...

    async def start(
        self,
        request: ExecutionRequest,
        context: ExecutionContext,
        cancellation: CancellationToken,
    ) -> ExecutionHandle: ...
