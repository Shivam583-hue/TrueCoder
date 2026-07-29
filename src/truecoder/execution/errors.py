from __future__ import annotations

from typing import TypeAlias

from .models import (
    BackendName,
    BackendPreference,
    NativeDiagnostic,
)

BackendCompatibilityFailures: TypeAlias = tuple[
    tuple[BackendName, tuple[str, ...]],
    ...,
]


class ExecutionInfrastructureError(Exception):
    """Base class for failures in the execution system itself. infrastructure errors"""

    def __init__(
        self,
        message: str,
        *,
        execution_id: str | None = None,
        backend: BackendName | None = None,
        operation: str | None = None,
        diagnostic: NativeDiagnostic | None = None,
    ) -> None:
        if not isinstance(message, str):
            raise TypeError("message must be a string")
        if not message.strip():
            raise ValueError("message cannot be empty or whitespace-only")

        if execution_id is not None:
            if not isinstance(execution_id, str):
                raise TypeError("execution_id must be a string or None")
            if not execution_id.strip():
                raise ValueError("execution_id cannot be empty or whitespace-only")

        if operation is not None:
            if not isinstance(operation, str):
                raise TypeError("operation must be a string or None")
            if not operation.strip():
                raise ValueError("operation cannot be empty or whitespace-only")

        if diagnostic is not None and not isinstance(
            diagnostic,
            NativeDiagnostic,
        ):
            raise TypeError("diagnostic must be a NativeDiagnostic instance or None")

        super().__init__(message)

        self.message = message
        self.execution_id = execution_id
        self.backend = backend
        self.operation = operation
        self.diagnostic = diagnostic


class BackendError(ExecutionInfrastructureError):
    """Base class for backend-related infrastructure failures."""


class BackendDiscoveryError(BackendError):
    """The system could not reliably determine backend availability."""


class BackendSelectionError(BackendError):
    """The system could not select a backend for the request."""


class BackendUnavailableError(BackendSelectionError):
    """A specifically requested backend is known but unavailable."""

    def __init__(
        self,
        message: str,
        *,
        preference: BackendPreference,
        execution_id: str | None = None,
        diagnostic: NativeDiagnostic | None = None,
    ) -> None:
        super().__init__(
            message,
            execution_id=execution_id,
            operation="select_backend",
            diagnostic=diagnostic,
        )
        self.preference = preference


class NoCompatibleBackendError(BackendSelectionError):
    """Available backends exist, but none can satisfy the request."""

    def __init__(
        self,
        message: str = "no compatible execution backend is available",
        *,
        failures: BackendCompatibilityFailures = (),
        preference: BackendPreference = "auto",
        execution_id: str | None = None,
    ) -> None:
        super().__init__(
            message,
            execution_id=execution_id,
            operation="select_backend",
        )
        self.failures = failures
        self.preference = preference


class BackendStartError(BackendError):
    """A backend was selected but could not start the execution.

    The service normally converts this into an ExecutionResult whose status is
    ``failed_to_start``.
    """


class BackendOperationError(BackendError):
    """A running backend failed during wait, communication, or monitoring."""


class BackendTerminationError(BackendOperationError):
    """The backend could not guarantee termination of the execution."""


class BackendCleanupError(BackendOperationError):
    """The backend could not release execution resources reliably."""


class OutputCollectionError(ExecutionInfrastructureError):
    """The service could not collect or process execution output reliably."""


class AuditError(ExecutionInfrastructureError):
    """Base class for durable audit subsystem failures."""


class AuditUnavailableError(AuditError):
    """The audit subsystem was unavailable before execution could begin."""


class AuditPersistenceError(AuditError):
    """An audit record or lifecycle event could not be persisted."""


class AuditRecoveryError(AuditError):
    """Startup recovery of a nonterminal audit record failed."""


class InvalidExecutionStateError(ExecutionInfrastructureError):
    """The service reached an impossible or inconsistent internal state."""
