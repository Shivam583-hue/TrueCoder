from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import TypeAlias

from truecoder.execution.models import BACKEND_NAMES

Metadata: TypeAlias = tuple[tuple[str, str], ...]
MAX_AUDIT_IDENTIFIER_CHARS = 512
MAX_AUDIT_METADATA_ITEMS = 64
MAX_AUDIT_METADATA_KEY_CHARS = 128
MAX_AUDIT_METADATA_VALUE_CHARS = 4096
MAX_AUDIT_PREVIEW_BYTES = 128 * 1024


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


class AuditEventType(str, Enum):
    RUN_CREATED = "run_created"
    POLICY_ALLOWED = "policy_allowed"
    POLICY_DENIED = "policy_denied"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    RESOURCE_RESERVED = "resource_reserved"
    BACKEND_STARTING = "backend_starting"
    BACKEND_STARTED = "backend_started"
    CANCELLATION_REQUESTED = "cancellation_requested"
    TIMEOUT_REACHED = "timeout_reached"
    LIMIT_REACHED = "limit_reached"
    TERMINATION_STARTED = "termination_started"
    CLEANUP_STARTED = "cleanup_started"
    CLEANUP_COMPLETED = "cleanup_completed"
    CLEANUP_FAILED = "cleanup_failed"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_RESOURCE_ABSENT = "recovery_resource_absent"
    RECOVERY_TERMINATED = "recovery_terminated"
    RECOVERY_FAILED = "recovery_failed"
    RUN_FINALIZED = "run_finalized"


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


def _validate_identifier(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > MAX_AUDIT_IDENTIFIER_CHARS:
        raise ValueError(
            f"{field_name} must not exceed {MAX_AUDIT_IDENTIFIER_CHARS} characters"
        )


def _validate_timestamp(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be expressed in UTC")


def _validate_metadata(value: object, field_name: str = "metadata") -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    if len(value) > MAX_AUDIT_METADATA_ITEMS:
        raise ValueError(
            f"{field_name} must not contain more than {MAX_AUDIT_METADATA_ITEMS} items"
        )

    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError(f"{field_name}[{index}] must be a two-item tuple")
        key, item_value = item
        _validate_identifier(key, f"{field_name}[{index}].key")
        if len(key) > MAX_AUDIT_METADATA_KEY_CHARS:
            raise ValueError(
                f"{field_name}[{index}].key must not exceed "
                f"{MAX_AUDIT_METADATA_KEY_CHARS} characters"
            )
        if not isinstance(item_value, str):
            raise TypeError(f"{field_name}[{index}].value must be a string")
        if len(item_value) > MAX_AUDIT_METADATA_VALUE_CHARS:
            raise ValueError(
                f"{field_name}[{index}].value must not exceed "
                f"{MAX_AUDIT_METADATA_VALUE_CHARS} characters"
            )
        if key in seen:
            raise ValueError(f"{field_name} contains duplicate key {key!r}")
        seen.add(key)


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
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("version must be an integer")
        if self.version < 1:
            raise ValueError("version must be at least one")
        _validate_identifier(self.backend, "backend")
        if self.backend not in BACKEND_NAMES:
            raise ValueError(f"unknown backend: {self.backend!r}")
        _validate_identifier(self.resource_kind, "resource_type")
        _validate_identifier(self.resource_id, "resource_id")
        _validate_identifier(self.ownership_token, "ownership_token")
        _validate_identifier(self.host_id, "host_id")
        _validate_timestamp(self.created_at_utc, "created_at_utc")
        _validate_metadata(self.native_details, "native_details")


@dataclass(frozen=True, slots=True)
class OutputEvidence:
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_preview: str = ""
    stderr_preview: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    complete: bool = True

    def __post_init__(self) -> None:
        _validate_sha256(self.stdout_sha256, "stdout_sha256")
        _validate_sha256(self.stderr_sha256, "stderr_sha256")

        for name, value in (
            ("stdout_bytes", self.stdout_bytes),
            ("stderr_bytes", self.stderr_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must not be negative")

        for name, value in (
            ("stdout_preview", self.stdout_preview),
            ("stderr_preview", self.stderr_preview),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
            if len(value.encode("utf-8")) > MAX_AUDIT_PREVIEW_BYTES:
                raise ValueError(
                    f"{name} must not exceed {MAX_AUDIT_PREVIEW_BYTES} UTF-8 bytes"
                )

        for name, value in (
            ("stdout_truncated", self.stdout_truncated),
            ("stderr_truncated", self.stderr_truncated),
            ("complete", self.complete),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be a boolean")

        if self.stdout_bytes and self.stdout_sha256 is None:
            raise ValueError("non-empty stdout requires stdout_sha256")
        if self.stderr_bytes and self.stderr_sha256 is None:
            raise ValueError("non-empty stderr requires stderr_sha256")


@dataclass(frozen=True, slots=True)
class AuditRunAdmission:
    run_id: str
    execution_id: str
    tool_call_id: str
    session_id: str
    turn_id: str
    workspace_id: str
    request_sha256: str
    request_summary: Metadata
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name, value in (
            ("run_id", self.run_id),
            ("execution_id", self.execution_id),
            ("tool_call_id", self.tool_call_id),
            ("session_id", self.session_id),
            ("turn_id", self.turn_id),
            ("workspace_id", self.workspace_id),
        ):
            _validate_identifier(value, field_name)
        _validate_sha256(self.request_sha256, "request_sha256")
        _validate_metadata(self.request_summary, "request_summary")
        _validate_timestamp(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class AuditRunHandle:
    run_id: str
    execution_id: str

    def __post_init__(self) -> None:
        _validate_identifier(self.run_id, "run_id")
        _validate_identifier(self.execution_id, "execution_id")


@dataclass(frozen=True, slots=True)
class AuditRunStart:
    run_id: str
    started_at: datetime
    resource: BackendResourceIdentifier | None = None
    metadata: Metadata = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.run_id, "run_id")
        _validate_timestamp(self.started_at, "started_at")
        if self.resource is not None and not isinstance(
            self.resource,
            BackendResourceIdentifier,
        ):
            raise TypeError("resource must be a BackendResourceIdentifier or None")
        _validate_metadata(self.metadata)


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
        if not isinstance(self.previous_phase, AuditRunPhase):
            raise TypeError("previous_phase must be an AuditRunPhase")
        if not isinstance(self.outcome, TerminalOutcome):
            raise TypeError("outcome must be a TerminalOutcome")
        if self.resource is not None and not isinstance(
            self.resource,
            BackendResourceIdentifier,
        ):
            raise TypeError("resource must be a BackendResourceIdentifier or None")
        if self.detail is not None:
            _validate_identifier(self.detail, "detail")

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
        if not isinstance(self.outcome, TerminalOutcome):
            raise TypeError("outcome must be a TerminalOutcome")
        if self.command_started is not None and not isinstance(
            self.command_started,
            bool,
        ):
            raise TypeError("command_started must be a boolean or None")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise TypeError("exit_code must be an integer or None")
        if self.output is not None and not isinstance(self.output, OutputEvidence):
            raise TypeError("output must be OutputEvidence or None")
        if self.resource is not None and not isinstance(
            self.resource,
            BackendResourceIdentifier,
        ):
            raise TypeError("resource must be BackendResourceIdentifier or None")
        if self.detail is not None:
            _validate_identifier(self.detail, "detail")

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
        if not self.command_started and self.output is not None:
            raise ValueError("A run that never started cannot contain output evidence")

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

        if self.outcome in {
            TerminalOutcome.TIMED_OUT,
            TerminalOutcome.CANCELLED,
            TerminalOutcome.LIMIT_EXCEEDED,
        }:
            if not self.command_started:
                raise ValueError(f"{self.outcome.value} requires command_started=True")
            if self.exit_code is not None:
                raise ValueError(f"{self.outcome.value} must not have an exit code")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    run_id: str
    sequence: int
    occurred_at: datetime
    phase: AuditRunPhase
    event_type: AuditEventType
    message: str | None = None
    metadata: Metadata = ()
    terminal: bool = False

    def __post_init__(self) -> None:
        _validate_identifier(self.event_id, "event_id")
        _validate_identifier(self.run_id, "run_id")
        _validate_timestamp(self.occurred_at, "occurred_at")
        if not isinstance(self.phase, AuditRunPhase):
            raise TypeError("phase must be an AuditRunPhase")
        if not isinstance(self.event_type, AuditEventType):
            raise TypeError("event_type must be an AuditEventType")

        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("sequence must be an integer")
        if self.sequence < 0:
            raise ValueError("sequence must not be negative")
        if self.message is not None:
            _validate_identifier(self.message, "message")
        _validate_metadata(self.metadata)
        if not isinstance(self.terminal, bool):
            raise TypeError("terminal must be a boolean")
        if self.terminal and (
            self.phase is not AuditRunPhase.TERMINAL
            or self.event_type is not AuditEventType.RUN_FINALIZED
        ):
            raise ValueError("terminal events must be terminal run_finalized events")
        if (
            self.phase is AuditRunPhase.TERMINAL
            and self.event_type is AuditEventType.RUN_FINALIZED
            and not self.terminal
        ):
            raise ValueError("run_finalized events must be terminal")


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
        if not isinstance(self.phase, AuditRunPhase):
            raise TypeError("phase must be an AuditRunPhase")

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


@dataclass(frozen=True, slots=True)
class AuditRunSnapshot:
    admission: AuditRunAdmission
    record: AuditRunRecord
    resource: BackendResourceIdentifier | None = None
    recovery_owner: str | None = None
    recovery_lease_until: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.admission, AuditRunAdmission):
            raise TypeError("admission must be an AuditRunAdmission")
        if not isinstance(self.record, AuditRunRecord):
            raise TypeError("record must be an AuditRunRecord")
        if self.admission.run_id != self.record.run_id:
            raise ValueError("admission and record run IDs must match")
        if self.resource is not None and not isinstance(
            self.resource,
            BackendResourceIdentifier,
        ):
            raise TypeError("resource must be BackendResourceIdentifier or None")
        if self.recovery_owner is not None:
            _validate_identifier(self.recovery_owner, "recovery_owner")
        if self.recovery_lease_until is not None:
            _validate_timestamp(
                self.recovery_lease_until,
                "recovery_lease_until",
            )
