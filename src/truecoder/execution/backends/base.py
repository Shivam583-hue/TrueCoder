from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol, TypeAlias, runtime_checkable

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
    """Start executions only after discovery and capability selection.

    A backend may acquire a native resource before calling
    ``register_resource``, but it must keep project-controlled code behind a
    launch gate until that awaited callback succeeds. If registration fails,
    the backend still owns the partial resource and must clean it before
    propagating the error. A returned handle therefore always has durable
    recovery identity.
    """

    @property
    def descriptor(self) -> BackendDescriptor: ...

    async def start(
        self,
        request: ExecutionRequest,
        context: ExecutionContext,
        cancellation: CancellationToken,
        register_resource: BackendResourceRegistrar,
    ) -> ExecutionHandle: ...
