from __future__ import annotations

from typing import TypeAlias

from .models import (
    BACKEND_NAMES,
    BACKEND_PREFERENCES,
    BackendName,
    BackendPreference,
    NativeDiagnostic,
)

BackendCompatibilityFailures: TypeAlias = tuple[
    tuple[BackendName, tuple[str, ...]],
    ...,
]


def _require_nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} cannot be empty or whitespace-only")
    return value


def _validate_backend_failures(
    failures: object,
) -> BackendCompatibilityFailures:
    if not isinstance(failures, tuple):
        raise TypeError("failures must be a tuple")

    seen_backends: set[str] = set()
    for index, item in enumerate(failures):
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError(f"failures[{index}] must be a two-item tuple")

        backend, reasons = item
        if backend not in BACKEND_NAMES:
            raise ValueError(f"unknown backend name: {backend!r}")
        if backend in seen_backends:
            raise ValueError(f"duplicate backend failure: {backend!r}")
        if not isinstance(reasons, tuple):
            raise TypeError(f"failures[{index}].reasons must be a tuple")
        if not reasons:
            raise ValueError(f"failures[{index}].reasons cannot be empty")

        seen_reasons: set[str] = set()
        for reason_index, reason in enumerate(reasons):
            reason = _require_nonempty_string(
                reason,
                f"failures[{index}].reasons[{reason_index}]",
            )
            if reason in seen_reasons:
                raise ValueError(
                    f"failures[{index}].reasons cannot contain duplicates"
                )
            seen_reasons.add(reason)

        seen_backends.add(backend)

    return failures


class ExecutionInfrastructureError(Exception):
    """Base class for failures in the execution system itself."""

    def __init__(
        self,
        message: str,
        *,
        execution_id: str | None = None,
        backend: BackendName | None = None,
        operation: str | None = None,
        diagnostic: NativeDiagnostic | None = None,
    ) -> None:
        _require_nonempty_string(message, "message")

        if execution_id is not None:
            _require_nonempty_string(execution_id, "execution_id")

        if backend is not None and backend not in BACKEND_NAMES:
            raise ValueError(f"unknown backend name: {backend!r}")

        if operation is not None:
            _require_nonempty_string(operation, "operation")

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
        if preference not in BACKEND_PREFERENCES:
            raise ValueError(f"unknown backend preference: {preference!r}")
        if preference == "auto":
            raise ValueError("BackendUnavailableError requires a specific preference")

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
        _validate_backend_failures(failures)
        if preference not in BACKEND_PREFERENCES:
            raise ValueError(f"unknown backend preference: {preference!r}")

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


class RequestValidationError(ExecutionInfrastructureError):
    """A request could not be converted into a valid execution contract."""


class PolicyEvaluationError(ExecutionInfrastructureError):
    """Execution policy could not produce a reliable decision."""


class ApprovalError(ExecutionInfrastructureError):
    """The approval interaction failed before it produced a decision."""


class EnvironmentConstructionError(ExecutionInfrastructureError):
    """The child environment could not be constructed safely."""


class ExecutionSerializationError(ExecutionInfrastructureError):
    """Serialized execution data is malformed, unknown, or unsupported."""


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
