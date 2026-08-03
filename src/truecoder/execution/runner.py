from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, TypeAlias

from truecoder.execution.audit.models import (
    AuditEventType,
    AuditFinalization,
    AuditRunHandle,
    AuditRunRecord,
    BackendResourceIdentifier,
    OutputEvidence,
    TerminalOutcome,
)
from truecoder.execution.audit.service import AuditService
from truecoder.execution.backends.base import ExecutionHandle
from truecoder.execution.backends.models import BackendExit, CleanupResult
from truecoder.execution.backends.registry import BackendRegistry
from truecoder.execution.cancellation import (
    CancellationRequested,
    CancellationSource,
    CancellationToken,
)
from truecoder.execution.errors import (
    AuditPersistenceError,
    AuditUnavailableError,
    BackendStartError,
    InvalidExecutionStateError,
)
from truecoder.execution.lifecycle import (
    LifecycleState,
    RunState,
    TerminalArbiter,
    TerminalClaim,
    resolve_terminal_claim,
)
from truecoder.execution.models import (
    BackendName,
    ExecutionContext,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    PolicyDecision,
    TerminationReason,
)
from truecoder.execution.output import CollectedOutput, OutputCollector, StreamOutput
from truecoder.execution.preparation import PreparedExecution
from truecoder.execution.registry import ActiveExecution, ExecutionRegistry

ApprovalGate: TypeAlias = Callable[
    [PreparedExecution, ExecutionContext],
    Awaitable[bool],
]
Monotonic: TypeAlias = Callable[[], float]

MAX_AUDIT_PREVIEW_BYTES: Final = 128 * 1024

_STATUS_BY_OUTCOME: Final[dict[TerminalOutcome, ExecutionStatus]] = {
    TerminalOutcome.COMPLETED: "completed",
    TerminalOutcome.FAILED: "failed",
    TerminalOutcome.TIMED_OUT: "timed_out",
    TerminalOutcome.CANCELLED: "cancelled",
    TerminalOutcome.LIMIT_EXCEEDED: "limit_exceeded",
    TerminalOutcome.POLICY_DENIED: "denied",
    TerminalOutcome.APPROVAL_REJECTED: "denied",
    TerminalOutcome.FAILED_TO_START: "failed_to_start",
}

_OUTCOME_BY_STATUS: Final[dict[ExecutionStatus, TerminalOutcome]] = {
    "completed": TerminalOutcome.COMPLETED,
    "failed": TerminalOutcome.FAILED,
    "timed_out": TerminalOutcome.TIMED_OUT,
    "cancelled": TerminalOutcome.CANCELLED,
    "limit_exceeded": TerminalOutcome.LIMIT_EXCEEDED,
    "denied": TerminalOutcome.POLICY_DENIED,
    "failed_to_start": TerminalOutcome.FAILED_TO_START,
}

# Outcomes whose audit rows forbid an exit code even though the backend
# eventually reports one.
_TERMINATED_OUTCOMES: Final = frozenset(
    {
        TerminalOutcome.TIMED_OUT,
        TerminalOutcome.CANCELLED,
        TerminalOutcome.LIMIT_EXCEEDED,
    }
)

_REASON_BEARING_STATUSES: Final = frozenset(
    {"timed_out", "cancelled", "limit_exceeded"}
)

_LIMIT_REASONS: Final[dict[str, TerminationReason]] = {
    "output_limit": "output_limit",
    "memory_limit": "memory_limit",
    "cpu_limit": "cpu_limit",
    "process_limit": "process_limit",
}


class _EvidenceLost(Exception):
    def __init__(self, boundary: str, cause: BaseException) -> None:
        self.boundary = boundary
        self.cause = cause
        super().__init__(boundary)


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

        for name, value in (
            ("started_at_monotonic", self.started_at_monotonic),
            ("finished_at_monotonic", self.finished_at_monotonic),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")

        if self.finished_at_monotonic is None:
            raise TypeError("finished_at_monotonic is required")

        if (
            self.started_at_monotonic is not None
            and self.finished_at_monotonic < self.started_at_monotonic
        ):
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
        return max(0.0, self.finished_at_monotonic - self.started_at_monotonic)


def empty_output() -> CollectedOutput:
    empty = StreamOutput(text="", byte_count=0, sha256=None, truncated=False)
    return CollectedOutput(
        stdout=empty,
        stderr=empty,
        complete=True,
        output_limit_exceeded=False,
        retained_bytes=0,
    )


def build_output_evidence(
    output: CollectedOutput,
    *,
    preview_budget: int = MAX_AUDIT_PREVIEW_BYTES,
) -> OutputEvidence:
    if not isinstance(output, CollectedOutput):
        raise TypeError("output must be a CollectedOutput")
    if isinstance(preview_budget, bool) or not isinstance(preview_budget, int):
        raise TypeError("preview_budget must be an integer")
    if preview_budget < 0:
        raise ValueError("preview_budget must not be negative")

    stdout_preview, stdout_clipped = _bounded_preview(
        output.stdout.text,
        preview_budget,
    )
    stderr_preview, stderr_clipped = _bounded_preview(
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
        complete=output.complete,
    )


def build_finalization(
    run_id: str,
    material: TerminalMaterial,
    *,
    finalized_at: datetime,
    resource: BackendResourceIdentifier | None = None,
    detail: str | None = None,
) -> AuditFinalization:
    if not isinstance(material, TerminalMaterial):
        raise TypeError("material must be a TerminalMaterial")

    outcome = _OUTCOME_BY_STATUS[material.claim.status]
    underlying: TerminalOutcome | None = None
    if material.cleanup is not None and not material.cleanup.complete:
        underlying = outcome
        outcome = TerminalOutcome.CLEANUP_FAILED

    exit_code = _finalization_exit_code(material, underlying or outcome)
    return AuditFinalization(
        run_id=run_id,
        finalized_at=finalized_at,
        outcome=outcome,
        command_started=material.command_started,
        exit_code=exit_code,
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

    finalization = record.finalization
    outcome = finalization.outcome
    if outcome is TerminalOutcome.CLEANUP_FAILED:
        assert finalization.underlying_outcome is not None
        outcome = finalization.underlying_outcome

    status = _STATUS_BY_OUTCOME.get(outcome)
    if status is None:
        raise ValueError(f"{outcome.value} cannot become a public execution result")

    reason = material.claim.reason if status in _REASON_BEARING_STATUSES else None
    return ExecutionResult(
        status=status,
        exit_code=_result_exit_code(status, material),
        stdout=material.output.stdout.text,
        stderr=material.output.stderr.text,
        duration_seconds=material.duration_seconds,
        stdout_bytes=material.output.stdout.byte_count,
        stderr_bytes=material.output.stderr.byte_count,
        stdout_truncated=material.output.stdout.truncated,
        stderr_truncated=material.output.stderr.truncated,
        termination_reason=reason,
        backend=backend if status != "denied" else None,
        audit_id=record.run_id,
    )


class ExecutionRunner:
    def __init__(
        self,
        audit: AuditService,
        backends: BackendRegistry,
        *,
        registry: ExecutionRegistry | None = None,
        approval_gate: ApprovalGate | None = None,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        if not isinstance(audit, AuditService):
            raise TypeError("audit must be an AuditService")
        if not isinstance(backends, BackendRegistry):
            raise TypeError("backends must be a BackendRegistry")
        if approval_gate is not None and not callable(approval_gate):
            raise TypeError("approval_gate must be callable")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")

        self._audit = audit
        self._backends = backends
        self._registry = registry or ExecutionRegistry()
        self._approval_gate = approval_gate
        self._monotonic = monotonic

    async def run(
        self,
        prepared: PreparedExecution,
        decision: PolicyDecision,
        context: ExecutionContext,
    ) -> ExecutionResult:
        if not isinstance(prepared, PreparedExecution):
            raise TypeError("prepared must be a PreparedExecution")
        if not isinstance(decision, PolicyDecision):
            raise TypeError("decision must be a PolicyDecision")
        if not isinstance(context, ExecutionContext):
            raise TypeError("context must be an ExecutionContext")

        state = LifecycleState(context.execution_id)
        handle = await self._admit(prepared.request, context)

        state.transition(RunState.POLICY_EVALUATED)
        if not decision.allowed:
            return await self._deny(handle, state, context, decision)

        await self._write_pre_start_event(
            handle,
            state,
            context,
            AuditEventType.POLICY_ALLOWED,
        )

        state.transition(RunState.PREPARED)
        if not await self._approve(handle, state, prepared, context):
            return await self._reject(handle, state, context)

        return await self._start_and_supervise(handle, state, prepared, context)

    async def _admit(
        self,
        request: ExecutionRequest,
        context: ExecutionContext,
    ) -> AuditRunHandle:
        try:
            return await self._audit.admit(context, request)
        except Exception as error:
            raise AuditUnavailableError(
                "execution was refused because pending evidence could not be stored",
                execution_id=context.execution_id,
                operation="admit",
            ) from error

    async def _deny(
        self,
        handle: AuditRunHandle,
        state: LifecycleState,
        context: ExecutionContext,
        decision: PolicyDecision,
    ) -> ExecutionResult:
        await self._write_pre_start_event(
            handle,
            state,
            context,
            AuditEventType.POLICY_DENIED,
            message=_first_reason(decision),
        )
        state.transition(RunState.FINALIZING)
        material = self._pre_start_material("denied", "policy_denied")
        record = await self._settle(handle, material, context)
        state.transition(RunState.TERMINAL)
        return build_execution_result(record, material, backend=None)

    async def _approve(
        self,
        handle: AuditRunHandle,
        state: LifecycleState,
        prepared: PreparedExecution,
        context: ExecutionContext,
    ) -> bool:
        if self._approval_gate is None:
            return True

        state.transition(RunState.AWAITING_APPROVAL)
        await self._write_pre_start_event(
            handle,
            state,
            context,
            AuditEventType.APPROVAL_REQUESTED,
        )
        approved = bool(await self._approval_gate(prepared, context))
        await self._write_pre_start_event(
            handle,
            state,
            context,
            AuditEventType.APPROVAL_GRANTED
            if approved
            else AuditEventType.APPROVAL_REJECTED,
        )
        return approved

    async def _reject(
        self,
        handle: AuditRunHandle,
        state: LifecycleState,
        context: ExecutionContext,
    ) -> ExecutionResult:
        state.transition(RunState.FINALIZING)
        material = self._pre_start_material("denied", "approval_rejected")
        record = await self._settle(
            handle,
            material,
            context,
            outcome_override=TerminalOutcome.APPROVAL_REJECTED,
        )
        state.transition(RunState.TERMINAL)
        return build_execution_result(record, material, backend=None)

    async def _start_and_supervise(
        self,
        handle: AuditRunHandle,
        state: LifecycleState,
        prepared: PreparedExecution,
        context: ExecutionContext,
    ) -> ExecutionResult:
        backend_name = prepared.backend.name
        source = CancellationSource()
        entry = ActiveExecution(context=context, cancellation_source=source)
        await self._registry.register(entry)
        state.transition(RunState.REGISTERED)

        try:
            try:
                await self._audit.append_event(
                    handle,
                    AuditEventType.BACKEND_STARTING,
                )
            except Exception as error:
                return await self._fail_to_start(
                    handle,
                    state,
                    context,
                    backend_name,
                    detail="starting_event_unavailable",
                    error=error,
                )

            state.transition(RunState.STARTING)
            return await self._supervise(
                handle,
                state,
                prepared,
                context,
                source.token,
            )
        finally:
            await self._registry.unregister(
                context.execution_id,
                expected=entry,
            )

    async def _supervise(
        self,
        handle: AuditRunHandle,
        state: LifecycleState,
        prepared: PreparedExecution,
        context: ExecutionContext,
        cancellation: CancellationToken,
    ) -> ExecutionResult:
        backend = self._backends.get_exact(
            prepared.backend,
            execution_id=context.execution_id,
        )
        attached: list[BackendResourceIdentifier] = []

        async def attach(resource: BackendResourceIdentifier) -> None:
            # A failure here keeps the backend's project gate closed, so the
            # backend aborts its own launch and no unrecorded process runs.
            await self._audit.attach_resource(handle, resource)
            attached.append(resource)

        try:
            execution = await backend.start(
                prepared,
                prepared.request,
                context,
                cancellation,
                attach,
            )
        except CancellationRequested as error:
            # The command never ran, so the durable row is failed_to_start,
            # but the caller still learns that this was a cancellation.
            return await self._fail_to_start(
                handle,
                state,
                context,
                prepared.backend.name,
                detail="cancelled_before_start",
                error=error,
                resource=attached[0] if attached else None,
                raise_original=True,
            )
        except Exception as error:
            return await self._fail_to_start(
                handle,
                state,
                context,
                prepared.backend.name,
                detail="backend_start_failed",
                error=error,
                resource=attached[0] if attached else None,
                reraise=not isinstance(error, BackendStartError),
            )

        started_at = self._monotonic()
        resource = execution.resource

        try:
            await self._audit.mark_running(handle, resource)
        except Exception as error:
            # The exact resource is already attached, so startup recovery can
            # find it even if this local cleanup is incomplete.
            await _terminate_quietly(execution, "shutdown")
            await _cleanup_quietly(execution)
            raise AuditPersistenceError(
                "execution was stopped because running evidence could not be stored",
                execution_id=context.execution_id,
                backend=prepared.backend.name,
                operation="mark_running",
            ) from error

        state.transition(RunState.RUNNING)
        return await self._await_terminal(
            handle,
            state,
            prepared,
            context,
            execution,
            cancellation,
            resource,
            started_at,
        )

    async def _await_terminal(
        self,
        handle: AuditRunHandle,
        state: LifecycleState,
        prepared: PreparedExecution,
        context: ExecutionContext,
        execution: ExecutionHandle,
        cancellation: CancellationToken,
        resource: BackendResourceIdentifier,
        started_at: float,
    ) -> ExecutionResult:
        limits = prepared.request.limits
        collector = OutputCollector(
            limits,
            redaction_values=prepared.environment.redaction_values,
        )
        limit_reached = asyncio.Event()
        drain = asyncio.create_task(
            _drain_output(execution, collector, limit_reached),
        )
        exit_task = asyncio.create_task(execution.wait())
        cancel_task = asyncio.create_task(cancellation.wait())
        timeout_task = asyncio.create_task(
            asyncio.sleep(limits.timeout_seconds),
        )
        limit_task = asyncio.create_task(limit_reached.wait())
        watched = {exit_task, cancel_task, timeout_task, limit_task}
        arbiter = TerminalArbiter()

        try:
            done, _pending = await asyncio.wait(
                watched,
                return_when=asyncio.FIRST_COMPLETED,
            )
            observed_at = self._monotonic()
            claim = resolve_terminal_claim(
                self._candidates(
                    done,
                    exit_task,
                    cancel_task,
                    timeout_task,
                    limit_task,
                    observed_at,
                ),
            )

            outcome = await arbiter.claim(claim)
            claim = outcome.claim

            if claim.source != "backend_exit":
                _enter_terminating(state)
                await self._runtime_event(
                    handle,
                    _EVENT_BY_SOURCE[claim.source],
                    context,
                )
                await self._runtime_event(
                    handle,
                    AuditEventType.TERMINATION_STARTED,
                    context,
                )
                await _terminate_quietly(
                    execution,
                    claim.reason or "cancellation",
                    limits.termination_grace_seconds,
                )

            backend_exit = await _settled_exit(exit_task)
            await _drain_quietly(drain)
            cleanup = await execution.cleanup()
            material = self._material(
                claim,
                backend_exit,
                collector.snapshot(),
                cleanup,
                started_at,
            )
        except _EvidenceLost as loss:
            material = await self._salvage(
                arbiter,
                execution,
                collector,
                drain,
                started_at,
            )
            _enter_terminating(state)
            await self._try_settle(
                handle,
                material,
                context,
                resource=resource,
                detail="runtime_evidence_lost",
            )
            raise AuditPersistenceError(
                "execution was stopped because runtime evidence could not be stored",
                execution_id=context.execution_id,
                backend=prepared.backend.name,
                operation=loss.boundary,
            ) from loss.cause
        finally:
            for task in (*watched, drain):
                task.cancel()
            await asyncio.gather(*watched, drain, return_exceptions=True)

        state.transition(RunState.FINALIZING)
        record = await self._settle(
            handle,
            material,
            context,
            resource=resource,
        )
        state.transition(RunState.TERMINAL)
        return build_execution_result(
            record,
            material,
            backend=prepared.backend.name,
        )

    def _candidates(
        self,
        done: set[asyncio.Task],
        exit_task: asyncio.Task,
        cancel_task: asyncio.Task,
        timeout_task: asyncio.Task,
        limit_task: asyncio.Task,
        observed_at: float,
    ) -> tuple[TerminalClaim, ...]:
        candidates: list[TerminalClaim] = []

        if exit_task in done and not exit_task.cancelled():
            error = exit_task.exception()
            if error is None:
                candidates.append(
                    _exit_claim(exit_task.result(), observed_at),
                )

        if limit_task in done:
            candidates.append(
                TerminalClaim(
                    status="limit_exceeded",
                    reason="output_limit",
                    observed_at_monotonic=observed_at,
                    source="output_limit",
                )
            )

        if cancel_task in done and not cancel_task.cancelled():
            reason: TerminationReason = (
                "shutdown"
                if cancel_task.exception() is None
                and str(cancel_task.result()).strip().lower() == "shutdown"
                else "cancellation"
            )
            candidates.append(
                TerminalClaim(
                    status="cancelled",
                    reason=reason,
                    observed_at_monotonic=observed_at,
                    source="cancellation",
                )
            )

        if timeout_task in done:
            candidates.append(
                TerminalClaim(
                    status="timed_out",
                    reason="timeout",
                    observed_at_monotonic=observed_at,
                    source="timeout",
                )
            )

        if not candidates:
            raise InvalidExecutionStateError(
                "the terminal wait returned no rankable signal",
                operation="await_terminal",
            )
        return tuple(candidates)

    async def _salvage(
        self,
        arbiter: TerminalArbiter,
        execution: ExecutionHandle,
        collector: OutputCollector,
        drain: asyncio.Task,
        started_at: float,
    ) -> TerminalMaterial:
        outcome = await arbiter.claim(
            TerminalClaim(
                status="cancelled",
                reason="shutdown",
                observed_at_monotonic=self._monotonic(),
                source="cancellation",
            )
        )
        claim = outcome.claim
        await _terminate_quietly(execution, "shutdown")
        await _drain_quietly(drain)
        cleanup = await _cleanup_quietly(execution)
        return self._material(
            claim,
            None,
            collector.snapshot(),
            cleanup,
            started_at,
        )

    async def _fail_to_start(
        self,
        handle: AuditRunHandle,
        state: LifecycleState,
        context: ExecutionContext,
        backend: BackendName,
        *,
        detail: str,
        error: BaseException,
        resource: BackendResourceIdentifier | None = None,
        reraise: bool = False,
        raise_original: bool = False,
    ) -> ExecutionResult:
        state.transition(RunState.FINALIZING)
        material = self._pre_start_material("failed_to_start", "failed_to_start")
        record = await self._try_settle(
            handle,
            material,
            context,
            resource=resource,
            detail=detail,
        )
        state.transition(RunState.TERMINAL)

        if raise_original:
            raise error

        if record is None or reraise:
            raise BackendStartError(
                "the execution could not be started",
                execution_id=context.execution_id,
                backend=backend,
                operation="start",
            ) from error

        return build_execution_result(record, material, backend=backend)

    async def _write_pre_start_event(
        self,
        handle: AuditRunHandle,
        state: LifecycleState,
        context: ExecutionContext,
        event_type: AuditEventType,
        *,
        message: str | None = None,
    ) -> None:
        try:
            await self._audit.append_event(
                handle,
                event_type,
                message=message,
            )
        except Exception as error:
            if not state.is_terminal and state.current is not RunState.FINALIZING:
                state.transition(RunState.FINALIZING)
            await self._try_settle(
                handle,
                self._pre_start_material("failed_to_start", "failed_to_start"),
                context,
                detail="pre_start_event_unavailable",
            )
            raise AuditPersistenceError(
                "execution was refused because pending evidence could not be stored",
                execution_id=context.execution_id,
                operation=f"append_{event_type.value}",
            ) from error

    async def _runtime_event(
        self,
        handle: AuditRunHandle,
        event_type: AuditEventType,
        context: ExecutionContext,
    ) -> None:
        try:
            await self._audit.append_event(handle, event_type)
        except Exception as error:
            raise _EvidenceLost(f"append_{event_type.value}", error) from error

    def _pre_start_material(
        self,
        status: ExecutionStatus,
        source: str,
    ) -> TerminalMaterial:
        output = empty_output()
        observed_at = self._monotonic()
        return TerminalMaterial(
            claim=TerminalClaim(
                status=status,
                reason=None,
                observed_at_monotonic=observed_at,
                source=source,
            ),
            backend_exit=None,
            output=output,
            audit_output=build_output_evidence(output),
            cleanup=None,
            started_at_monotonic=None,
            finished_at_monotonic=observed_at,
        )

    def _material(
        self,
        claim: TerminalClaim,
        backend_exit: BackendExit | None,
        output: CollectedOutput,
        cleanup: CleanupResult | None,
        started_at: float,
    ) -> TerminalMaterial:
        return TerminalMaterial(
            claim=claim,
            backend_exit=backend_exit,
            output=output,
            audit_output=build_output_evidence(output),
            cleanup=cleanup,
            started_at_monotonic=started_at,
            finished_at_monotonic=self._monotonic(),
        )

    async def _try_settle(
        self,
        handle: AuditRunHandle,
        material: TerminalMaterial,
        context: ExecutionContext,
        *,
        resource: BackendResourceIdentifier | None = None,
        detail: str | None = None,
    ) -> AuditRunRecord | None:
        try:
            return await self._settle(
                handle,
                material,
                context,
                resource=resource,
                detail=detail,
            )
        except AuditPersistenceError:
            # The pending row survives for startup recovery; never fabricate a
            # terminal row in memory.
            return None

    async def _settle(
        self,
        handle: AuditRunHandle,
        material: TerminalMaterial,
        context: ExecutionContext,
        *,
        resource: BackendResourceIdentifier | None = None,
        detail: str | None = None,
        outcome_override: TerminalOutcome | None = None,
    ) -> AuditRunRecord:
        finalization = build_finalization(
            handle.run_id,
            material,
            finalized_at=self._audit.now(),
            resource=resource,
            detail=detail,
        )
        if outcome_override is not None:
            finalization = _with_outcome(finalization, outcome_override)

        try:
            return await self._audit.finalize(handle, finalization)
        except Exception as error:
            raise AuditPersistenceError(
                "the execution result was withheld because its terminal "
                "evidence could not be stored",
                execution_id=context.execution_id,
                operation="finalize",
            ) from error


def _enter_terminating(state: LifecycleState) -> None:
    if state.current is RunState.RUNNING:
        state.transition(RunState.TERMINATING)


_EVENT_BY_SOURCE: Final[dict[str, AuditEventType]] = {
    "output_limit": AuditEventType.LIMIT_REACHED,
    "resource_limit": AuditEventType.LIMIT_REACHED,
    "cancellation": AuditEventType.CANCELLATION_REQUESTED,
    "timeout": AuditEventType.TIMEOUT_REACHED,
}


def _with_outcome(
    finalization: AuditFinalization,
    outcome: TerminalOutcome,
) -> AuditFinalization:
    return AuditFinalization(
        run_id=finalization.run_id,
        finalized_at=finalization.finalized_at,
        outcome=outcome,
        command_started=finalization.command_started,
        exit_code=finalization.exit_code,
        output=finalization.output,
        resource=finalization.resource,
        underlying_outcome=finalization.underlying_outcome,
        detail=finalization.detail,
    )


def _exit_claim(exit_status: BackendExit, observed_at: float) -> TerminalClaim:
    reason = exit_status.native_reason
    limit = _LIMIT_REASONS.get(reason) if reason is not None else None
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


def _finalization_exit_code(
    material: TerminalMaterial,
    outcome: TerminalOutcome,
) -> int | None:
    if not material.command_started or outcome in _TERMINATED_OUTCOMES:
        return None
    if material.backend_exit is None:
        return None
    return material.backend_exit.exit_code


def _result_exit_code(
    status: ExecutionStatus,
    material: TerminalMaterial,
) -> int | None:
    if status in {"denied", "failed_to_start"} or status in _REASON_BEARING_STATUSES:
        return None
    return material.backend_exit.exit_code if material.backend_exit else None


def _bounded_preview(text: str, budget: int) -> tuple[str, bool]:
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


def _first_reason(decision: PolicyDecision) -> str | None:
    for reason in decision.reasons:
        return reason.message
    return None


async def _drain_output(
    execution: ExecutionHandle,
    collector: OutputCollector,
    limit_reached: asyncio.Event,
) -> None:
    async for chunk in execution.output():
        update = (
            collector.feed_stdout(chunk.data)
            if chunk.stream == "stdout"
            else collector.feed_stderr(chunk.data)
        )
        if update.newly_exceeded:
            limit_reached.set()
    collector.close_stdout()
    collector.close_stderr()


async def _drain_quietly(drain: asyncio.Task) -> None:
    try:
        await drain
    except (asyncio.CancelledError, Exception):
        pass


async def _settled_exit(exit_task: asyncio.Task) -> BackendExit | None:
    try:
        return await exit_task
    except (asyncio.CancelledError, Exception):
        return None


async def _terminate_quietly(
    execution: ExecutionHandle,
    reason: TerminationReason,
    grace_seconds: float = 0.0,
) -> None:
    try:
        await execution.terminate(reason, grace_seconds)
    except Exception:
        pass


async def _cleanup_quietly(execution: ExecutionHandle) -> CleanupResult | None:
    try:
        return await execution.cleanup()
    except Exception:
        return None


