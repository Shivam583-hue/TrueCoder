from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import TypeAlias

Metadata: TypeAlias = tuple[tuple[str, str], ...]


class AuditRunPhase(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    TERMINAL = "terminal"


class TerminalOutcome(str, Enum):
    POLICY_DENIED = "policy_denied"
    APPROVAL_REJECTED = "approval_rejected"
    FAILED_TO_START = "failed_to_start"

    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    LIMIT_EXCEEDED = "limit_exceeded"
    CLEANUP_FAILED = "cleanup_failed"

    RECOVERED_NO_RESOURCE = "recovered_no_resource"
    RECOVERED_RESOURCE_ABSENT = "recovered_resource_absent"
    RECOVERED_TERMINATED = "recovered_terminated"
    RECOVERY_FAILED = "recovery_failed"


RECOVERY_OUTCOMES = frozenset(
    {
        TerminalOutcome.RECOVERED_NO_RESOURCE,
        TerminalOutcome.RECOVERED_RESOURCE_ABSENT,
        TerminalOutcome.RECOVERED_TERMINATED,
        TerminalOutcome.RECOVERY_FAILED,
    }
)

PRE_EXECUTION_OUTCOMES = frozenset(
    {
        TerminalOutcome.POLICY_DENIED,
        TerminalOutcome.APPROVAL_REJECTED,
        TerminalOutcome.FAILED_TO_START,
    }
)

_ALLOWED_TRANSITIONS: dict[AuditRunPhase, frozenset[AuditRunPhase]] = {
    AuditRunPhase.PENDING: frozenset(
        {
            AuditRunPhase.RUNNING,
            AuditRunPhase.TERMINAL,
        }
    ),
    AuditRunPhase.RUNNING: frozenset(
        {
            AuditRunPhase.TERMINAL,
        }
    ),
    AuditRunPhase.TERMINAL: frozenset(),
}


def can_transition(
    current: AuditRunPhase,
    target: AuditRunPhase,
) -> bool:
    return target in _ALLOWED_TRANSITIONS[current]


def validate_transition(
    current: AuditRunPhase,
    target: AuditRunPhase,
) -> None:
    if not can_transition(current, target):
        raise ValueError(
            f"Invalid audit run transition: {current.value} -> {target.value}"
        )


def _validate_identifier(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _validate_timestamp(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _validate_sha256(value: str | None, field_name: str) -> None:
    if value is None:
        return

    if len(value) != 64:
        raise ValueError(f"{field_name} must contain a 64-character SHA-256 digest")

    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be hexadecimal") from exc


@dataclass(frozen=True, slots=True)
class BackendResourceIdentifier:
    version: int
    backend: str
    resource_kind: str
    resource_id: str
    ownership_token: str
    host_id: str
    created_at_utc: datetime
    native_details: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _validate_identifier(self.backend, "backend")
        _validate_identifier(self.resource_kind, "resource_type")
        _validate_identifier(self.resource_id, "resource_id")


@dataclass(frozen=True, slots=True)
class OutputEvidence:
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def __post_init__(self) -> None:
        _validate_sha256(self.stdout_sha256, "stdout_sha256")
        _validate_sha256(self.stderr_sha256, "stderr_sha256")

        if self.stdout_bytes < 0:
            raise ValueError("stdout_bytes must not be negative")

        if self.stderr_bytes < 0:
            raise ValueError("stderr_bytes must not be negative")


@dataclass(frozen=True, slots=True)
class AuditRunStart:
    run_id: str
    started_at: datetime
    resource: BackendResourceIdentifier | None = None
    metadata: Metadata = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.run_id, "run_id")
        _validate_timestamp(self.started_at, "started_at")


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    run_id: str
    previous_phase: AuditRunPhase
    attempted_at: datetime
    outcome: TerminalOutcome
    resource: BackendResourceIdentifier | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.run_id, "run_id")
        _validate_timestamp(self.attempted_at, "attempted_at")

        if self.previous_phase is AuditRunPhase.TERMINAL:
            raise ValueError(
                "Recovery may only be applied to a previously nonterminal run"
            )

        if self.outcome not in RECOVERY_OUTCOMES:
            raise ValueError(f"{self.outcome.value!r} is not a recovery outcome")

        if (
            self.outcome is TerminalOutcome.RECOVERED_NO_RESOURCE
            and self.resource is not None
        ):
            raise ValueError(
                "recovered_no_resource must not contain a resource identifier"
            )

        if (
            self.outcome
            in {
                TerminalOutcome.RECOVERED_RESOURCE_ABSENT,
                TerminalOutcome.RECOVERED_TERMINATED,
            }
            and self.resource is None
        ):
            raise ValueError(f"{self.outcome.value} requires a resource identifier")


@dataclass(frozen=True, slots=True)
class AuditFinalization:
    run_id: str
    finalized_at: datetime
    outcome: TerminalOutcome

    # None is used for recovery finalizations because command execution
    # belongs to the previous process and may not be safely reconstructed.
    command_started: bool | None

    exit_code: int | None = None
    output: OutputEvidence | None = None
    resource: BackendResourceIdentifier | None = None

    # Required when outcome is cleanup_failed so the expected underlying
    # command outcome is not lost.
    underlying_outcome: TerminalOutcome | None = None

    # Required for recovery-only terminal outcomes.
    recovery: RecoveryResult | None = None

    detail: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.run_id, "run_id")
        _validate_timestamp(self.finalized_at, "finalized_at")

        self._validate_recovery()
        self._validate_cleanup_failure()
        self._validate_execution_semantics()

    def _validate_recovery(self) -> None:
        is_recovery_outcome = self.outcome in RECOVERY_OUTCOMES

        if is_recovery_outcome:
            if self.recovery is None:
                raise ValueError(f"{self.outcome.value} requires a RecoveryResult")

            if self.recovery.run_id != self.run_id:
                raise ValueError("RecoveryResult run_id must match finalization run_id")

            if self.recovery.outcome is not self.outcome:
                raise ValueError(
                    "RecoveryResult outcome must match finalization outcome"
                )

            if self.command_started is not None:
                raise ValueError("command_started must be None for recovery outcomes")

            if self.exit_code is not None:
                raise ValueError("exit_code must be None for recovery outcomes")

            if self.resource != self.recovery.resource:
                raise ValueError(
                    "Finalization resource must match RecoveryResult resource"
                )

        elif self.recovery is not None:
            raise ValueError("RecoveryResult may only accompany a recovery outcome")

    def _validate_cleanup_failure(self) -> None:
        if self.outcome is TerminalOutcome.CLEANUP_FAILED:
            if self.underlying_outcome is None:
                raise ValueError("cleanup_failed requires an underlying_outcome")

            if self.underlying_outcome is TerminalOutcome.CLEANUP_FAILED:
                raise ValueError("cleanup_failed cannot be its own underlying outcome")

            if self.underlying_outcome in RECOVERY_OUTCOMES:
                raise ValueError(
                    "Recovery outcomes cannot be underlying cleanup outcomes"
                )

        elif self.underlying_outcome is not None:
            raise ValueError("underlying_outcome is only valid for cleanup_failed")

    def _validate_execution_semantics(self) -> None:
        if self.outcome in RECOVERY_OUTCOMES:
            return

        if self.command_started is None:
            raise ValueError(
                "command_started must be specified for non-recovery outcomes"
            )

        if not self.command_started and self.exit_code is not None:
            raise ValueError("A run that never started cannot have an exit code")

        if self.outcome in PRE_EXECUTION_OUTCOMES:
            if self.command_started:
                raise ValueError(f"{self.outcome.value} requires command_started=False")

            if self.exit_code is not None:
                raise ValueError(f"{self.outcome.value} must not have an exit code")

        if self.outcome is TerminalOutcome.COMPLETED:
            if not self.command_started:
                raise ValueError("completed requires command_started=True")

            if self.exit_code != 0:
                raise ValueError("completed requires exit_code=0")

        if self.outcome is TerminalOutcome.FAILED:
            if not self.command_started:
                raise ValueError("failed requires command_started=True")

            if self.exit_code is None or self.exit_code == 0:
                raise ValueError("failed requires a nonzero exit code")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    run_id: str
    sequence: int
    occurred_at: datetime
    phase: AuditRunPhase
    event_type: str
    message: str | None = None
    metadata: Metadata = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.event_id, "event_id")
        _validate_identifier(self.run_id, "run_id")
        _validate_identifier(self.event_type, "event_type")
        _validate_timestamp(self.occurred_at, "occurred_at")

        if self.sequence < 0:
            raise ValueError("sequence must not be negative")


@dataclass(frozen=True, slots=True)
class AuditRunRecord:
    run_id: str
    created_at: datetime
    updated_at: datetime
    phase: AuditRunPhase = AuditRunPhase.PENDING
    start: AuditRunStart | None = None
    finalization: AuditFinalization | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        _validate_identifier(self.run_id, "run_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_timestamp(self.updated_at, "updated_at")

        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")

        if self.revision < 0:
            raise ValueError("revision must not be negative")

        if self.start is not None and self.start.run_id != self.run_id:
            raise ValueError("AuditRunStart run_id must match AuditRunRecord run_id")

        if self.finalization is not None and self.finalization.run_id != self.run_id:
            raise ValueError(
                "AuditFinalization run_id must match AuditRunRecord run_id"
            )

        if self.phase is AuditRunPhase.PENDING:
            if self.start is not None:
                raise ValueError("A pending run must not have an AuditRunStart")

            if self.finalization is not None:
                raise ValueError("A pending run must not have an AuditFinalization")

        elif self.phase is AuditRunPhase.RUNNING:
            if self.start is None:
                raise ValueError("A running run requires an AuditRunStart")

            if self.finalization is not None:
                raise ValueError("A running run must not have an AuditFinalization")

        elif self.phase is AuditRunPhase.TERMINAL:
            if self.finalization is None:
                raise ValueError("A terminal run requires an AuditFinalization")

        if self.start is not None:
            if self.start.started_at < self.created_at:
                raise ValueError("started_at must not precede created_at")

            if self.start.started_at > self.updated_at:
                raise ValueError("started_at must not follow updated_at")

        if self.finalization is not None:
            if self.finalization.finalized_at < self.created_at:
                raise ValueError("finalized_at must not precede created_at")

            if self.finalization.finalized_at != self.updated_at:
                raise ValueError(
                    "A terminal record's updated_at must equal finalized_at"
                )

            if (
                self.start is not None
                and self.finalization.finalized_at < self.start.started_at
            ):
                raise ValueError("finalized_at must not precede started_at")

    @property
    def outcome(self) -> TerminalOutcome | None:
        if self.finalization is None:
            return None

        return self.finalization.outcome

    def mark_running(self, start: AuditRunStart) -> AuditRunRecord:
        validate_transition(self.phase, AuditRunPhase.RUNNING)

        if start.run_id != self.run_id:
            raise ValueError("AuditRunStart run_id must match AuditRunRecord run_id")

        if start.started_at < self.updated_at:
            raise ValueError("started_at must not precede the record's updated_at")

        return replace(
            self,
            phase=AuditRunPhase.RUNNING,
            start=start,
            updated_at=start.started_at,
            revision=self.revision + 1,
        )

    def mark_terminal(
        self,
        finalization: AuditFinalization,
    ) -> AuditRunRecord:
        validate_transition(self.phase, AuditRunPhase.TERMINAL)

        if finalization.run_id != self.run_id:
            raise ValueError(
                "AuditFinalization run_id must match AuditRunRecord run_id"
            )

        if finalization.finalized_at < self.updated_at:
            raise ValueError("finalized_at must not precede the record's updated_at")

        if (
            finalization.outcome in RECOVERY_OUTCOMES
            and finalization.recovery is not None
            and finalization.recovery.previous_phase is not self.phase
        ):
            raise ValueError(
                "RecoveryResult previous_phase must match the current phase"
            )

        return replace(
            self,
            phase=AuditRunPhase.TERMINAL,
            finalization=finalization,
            updated_at=finalization.finalized_at,
            revision=self.revision + 1,
        )
