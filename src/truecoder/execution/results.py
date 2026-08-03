from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from truecoder.execution.audit.models import (
    AuditFinalization,
    AuditRunRecord,
    BackendResourceIdentifier,
    OutputEvidence,
    TerminalOutcome,
)
from truecoder.execution.backends.models import BackendExit, CleanupResult
from truecoder.execution.lifecycle import TerminalClaim
from truecoder.execution.models import (
    BackendName,
    ExecutionLifecycleStage,
    ExecutionResult,
    ExecutionStatus,
    TerminationReason,
)
from truecoder.execution.output import CollectedOutput, StreamOutput

MAX_AUDIT_PREVIEW_BYTES: Final = 128 * 1024

CANCELLED_BEFORE_START: Final = "cancelled_before_start"

OUTCOME_BY_STATUS: Final[dict[ExecutionStatus, TerminalOutcome]] = {
    "completed": TerminalOutcome.COMPLETED,
    "failed": TerminalOutcome.FAILED,
    "timed_out": TerminalOutcome.TIMED_OUT,
    "cancelled": TerminalOutcome.CANCELLED,
    "limit_exceeded": TerminalOutcome.LIMIT_EXCEEDED,
    "denied": TerminalOutcome.POLICY_DENIED,
    "failed_to_start": TerminalOutcome.FAILED_TO_START,
}

STATUS_BY_OUTCOME: Final[dict[TerminalOutcome, ExecutionStatus]] = {
    TerminalOutcome.COMPLETED: "completed",
    TerminalOutcome.FAILED: "failed",
    TerminalOutcome.TIMED_OUT: "timed_out",
    TerminalOutcome.CANCELLED: "cancelled",
    TerminalOutcome.LIMIT_EXCEEDED: "limit_exceeded",
    TerminalOutcome.POLICY_DENIED: "denied",
    TerminalOutcome.APPROVAL_REJECTED: "denied",
    TerminalOutcome.FAILED_TO_START: "failed_to_start",
}

TERMINAL_STAGE_BY_STATUS: Final[dict[ExecutionStatus, ExecutionLifecycleStage]] = {
    "completed": "completed",
    "failed": "failed",
    "timed_out": "timed_out",
    "cancelled": "cancelled",
    "limit_exceeded": "limit_exceeded",
    "denied": "denied",
    "failed_to_start": "failed_to_start",
}

REASON_BEARING_STATUSES: Final = frozenset(
    {"timed_out", "cancelled", "limit_exceeded"}
)

_NO_EXIT_CODE_STATUSES: Final = REASON_BEARING_STATUSES | {
    "denied",
    "failed_to_start",
}

_TERMINATED_OUTCOMES: Final = frozenset(
    {
        TerminalOutcome.TIMED_OUT,
        TerminalOutcome.CANCELLED,
        TerminalOutcome.LIMIT_EXCEEDED,
    }
)

LIMIT_REASONS: Final[dict[str, TerminationReason]] = {
    "output_limit": "output_limit",
    "memory_limit": "memory_limit",
    "cpu_limit": "cpu_limit",
    "process_limit": "process_limit",
}


@dataclass(frozen=True, slots=True)
class TerminalMaterial:
    claim: TerminalClaim
    backend_exit: BackendExit | None
    output: CollectedOutput
    audit_output: OutputEvidence
    cleanup: CleanupResult | None
    started_at_monotonic: float | None
    finished_at_monotonic: float

    def __post_init__(self) -> None:
        if not isinstance(self.claim, TerminalClaim):
            raise TypeError("claim must be a TerminalClaim")
        if self.backend_exit is not None and not isinstance(
            self.backend_exit,
            BackendExit,
        ):
            raise TypeError("backend_exit must be a BackendExit or None")
        if not isinstance(self.output, CollectedOutput):
            raise TypeError("output must be a CollectedOutput")
        if not isinstance(self.audit_output, OutputEvidence):
            raise TypeError("audit_output must be an OutputEvidence")
        if self.cleanup is not None and not isinstance(self.cleanup, CleanupResult):
            raise TypeError("cleanup must be a CleanupResult or None")

        _require_number(self.finished_at_monotonic, "finished_at_monotonic")
        if self.started_at_monotonic is not None:
            _require_number(self.started_at_monotonic, "started_at_monotonic")
            if self.finished_at_monotonic < self.started_at_monotonic:
                raise ValueError("finished_at_monotonic must not precede the start")

        if self.started_at_monotonic is None and self.backend_exit is not None:
            raise ValueError("a run that never started cannot have a backend exit")

    @property
    def command_started(self) -> bool:
        return self.started_at_monotonic is not None

    @property
    def duration_seconds(self) -> float:
        if self.started_at_monotonic is None:
            return 0.0
        return self.finished_at_monotonic - self.started_at_monotonic

    @property
    def cleanup_incomplete(self) -> bool:
        return self.cleanup is not None and not self.cleanup.complete


def empty_output(*, complete: bool = True) -> CollectedOutput:
    empty = StreamOutput(text="", byte_count=0, sha256=None, truncated=False)
    return CollectedOutput(
        stdout=empty,
        stderr=empty,
        complete=complete,
        output_limit_exceeded=False,
        retained_bytes=0,
    )


def build_output_evidence(
    output: CollectedOutput,
    *,
    complete: bool | None = None,
    preview_budget: int = MAX_AUDIT_PREVIEW_BYTES,
) -> OutputEvidence:
    if not isinstance(output, CollectedOutput):
        raise TypeError("output must be a CollectedOutput")
    if complete is not None and not isinstance(complete, bool):
        raise TypeError("complete must be a boolean or None")
    if isinstance(preview_budget, bool) or not isinstance(preview_budget, int):
        raise TypeError("preview_budget must be an integer")
    if preview_budget < 0:
        raise ValueError("preview_budget must not be negative")

    stdout_preview, stdout_clipped = bounded_preview(
        output.stdout.text,
        preview_budget,
    )
    stderr_preview, stderr_clipped = bounded_preview(
        output.stderr.text,
        preview_budget,
    )
    return OutputEvidence(
        stdout_sha256=output.stdout.sha256,
        stderr_sha256=output.stderr.sha256,
        stdout_bytes=output.stdout.byte_count,
        stderr_bytes=output.stderr.byte_count,
        stdout_preview=stdout_preview,
        stderr_preview=stderr_preview,
        stdout_truncated=output.stdout.truncated or stdout_clipped,
        stderr_truncated=output.stderr.truncated or stderr_clipped,
        complete=output.complete if complete is None else complete,
    )


def build_finalization(
    run_id: str,
    material: TerminalMaterial,
    *,
    finalized_at: datetime,
    resource: BackendResourceIdentifier | None = None,
    detail: str | None = None,
    outcome_override: TerminalOutcome | None = None,
) -> AuditFinalization:
    if not isinstance(material, TerminalMaterial):
        raise TypeError("material must be a TerminalMaterial")
    if outcome_override is not None and not isinstance(
        outcome_override,
        TerminalOutcome,
    ):
        raise TypeError("outcome_override must be a TerminalOutcome or None")

    outcome = outcome_override or OUTCOME_BY_STATUS[material.claim.status]
    underlying: TerminalOutcome | None = None
    if material.cleanup_incomplete:
        underlying = outcome
        outcome = TerminalOutcome.CLEANUP_FAILED

    return AuditFinalization(
        run_id=run_id,
        finalized_at=finalized_at,
        outcome=outcome,
        command_started=material.command_started,
        exit_code=finalization_exit_code(material, underlying or outcome),
        output=material.audit_output if material.command_started else None,
        resource=resource,
        underlying_outcome=underlying,
        detail=detail,
    )


def build_execution_result(
    record: AuditRunRecord,
    material: TerminalMaterial,
    *,
    backend: BackendName | None,
) -> ExecutionResult:
    if not isinstance(record, AuditRunRecord):
        raise TypeError("record must be an AuditRunRecord")
    if not isinstance(material, TerminalMaterial):
        raise TypeError("material must be a TerminalMaterial")
    if record.finalization is None:
        raise ValueError("a terminal audit record requires a finalization")

    status = public_status(record.finalization)
    return ExecutionResult(
        status=status,
        exit_code=public_exit_code(status, material),
        stdout=material.output.stdout.text,
        stderr=material.output.stderr.text,
        duration_seconds=material.duration_seconds,
        stdout_bytes=material.output.stdout.byte_count,
        stderr_bytes=material.output.stderr.byte_count,
        stdout_truncated=material.output.stdout.truncated,
        stderr_truncated=material.output.stderr.truncated,
        termination_reason=public_termination_reason(status, material),
        backend=None if status == "denied" else backend,
        audit_id=record.run_id,
    )


def build_cancelled_before_start_result(
    record: AuditRunRecord,
    material: TerminalMaterial,
    *,
    backend: BackendName,
    reason: TerminationReason,
) -> ExecutionResult:
    if record.finalization is None:
        raise ValueError("a terminal audit record requires a finalization")
    if record.finalization.outcome is not TerminalOutcome.FAILED_TO_START:
        raise ValueError(
            "a pre-start cancellation must be recorded as failed_to_start"
        )
    if reason not in {"cancellation", "shutdown"}:
        raise ValueError(f"unknown cancellation reason: {reason!r}")

    return ExecutionResult(
        status="cancelled",
        exit_code=None,
        stdout=material.output.stdout.text,
        stderr=material.output.stderr.text,
        duration_seconds=material.duration_seconds,
        stdout_bytes=material.output.stdout.byte_count,
        stderr_bytes=material.output.stderr.byte_count,
        stdout_truncated=material.output.stdout.truncated,
        stderr_truncated=material.output.stderr.truncated,
        termination_reason=reason,
        backend=backend,
        audit_id=record.run_id,
    )


def cancellation_reason(text: str) -> TerminationReason:
    return "shutdown" if text.strip().lower() == "shutdown" else "cancellation"


def public_status(finalization: AuditFinalization) -> ExecutionStatus:
    outcome = finalization.outcome
    if outcome is TerminalOutcome.CLEANUP_FAILED:
        if finalization.underlying_outcome is None:
            raise ValueError("cleanup_failed requires an underlying outcome")
        outcome = finalization.underlying_outcome

    status = STATUS_BY_OUTCOME.get(outcome)
    if status is None:
        raise ValueError(f"{outcome.value} cannot become a public execution result")
    return status


def public_exit_code(
    status: ExecutionStatus,
    material: TerminalMaterial,
) -> int | None:
    if status in _NO_EXIT_CODE_STATUSES:
        return None
    if material.backend_exit is None:
        return None
    return material.backend_exit.exit_code


def public_termination_reason(
    status: ExecutionStatus,
    material: TerminalMaterial,
) -> TerminationReason | None:
    if status not in REASON_BEARING_STATUSES:
        return None
    return material.claim.reason


def finalization_exit_code(
    material: TerminalMaterial,
    outcome: TerminalOutcome,
) -> int | None:
    if not material.command_started or outcome in _TERMINATED_OUTCOMES:
        return None
    if material.backend_exit is None:
        return None
    return material.backend_exit.exit_code


def claim_for_exit(exit_status: BackendExit, observed_at: float) -> TerminalClaim:
    if not isinstance(exit_status, BackendExit):
        raise TypeError("exit_status must be a BackendExit")

    reason = exit_status.native_reason
    limit = LIMIT_REASONS.get(reason) if reason is not None else None
    if limit is not None:
        return TerminalClaim(
            status="limit_exceeded",
            reason=limit,
            observed_at_monotonic=observed_at,
            source="resource_limit",
        )
    if exit_status.exit_code is None:
        return TerminalClaim(
            status="cancelled",
            reason="shutdown" if reason == "shutdown" else "cancellation",
            observed_at_monotonic=observed_at,
            source="backend_exit",
        )
    return TerminalClaim(
        status="completed" if exit_status.exit_code == 0 else "failed",
        reason=None,
        observed_at_monotonic=observed_at,
        source="backend_exit",
    )


def claim_for_cancellation(reason: str, observed_at: float) -> TerminalClaim:
    return TerminalClaim(
        status="cancelled",
        reason="shutdown" if reason.strip().lower() == "shutdown" else "cancellation",
        observed_at_monotonic=observed_at,
        source="cancellation",
    )


def claim_for_timeout(observed_at: float) -> TerminalClaim:
    return TerminalClaim(
        status="timed_out",
        reason="timeout",
        observed_at_monotonic=observed_at,
        source="timeout",
    )


def claim_for_output_limit(observed_at: float) -> TerminalClaim:
    return TerminalClaim(
        status="limit_exceeded",
        reason="output_limit",
        observed_at_monotonic=observed_at,
        source="output_limit",
    )


def bounded_preview(text: str, budget: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text, False

    kept: list[str] = []
    used = 0
    for character in text:
        size = len(character.encode("utf-8"))
        if used + size > budget:
            break
        kept.append(character)
        used += size
    return "".join(kept), True


def _require_number(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
