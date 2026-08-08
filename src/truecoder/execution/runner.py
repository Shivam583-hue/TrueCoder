from __future__ import annotations

import asyncio
import shlex
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Final, Protocol, TypeAlias, runtime_checkable

from truecoder.execution.audit.models import (
    AuditEventType,
    AuditRunHandle,
    AuditRunRecord,
    BackendResourceIdentifier,
    TerminalOutcome,
)
from truecoder.execution.audit.service import AuditService
from truecoder.execution.backends.base import (
    BackendStartContext,
    ExecutionHandle,
)
from truecoder.execution.backends.models import BackendExit, CleanupResult
from truecoder.execution.backends.registry import BackendRegistry
from truecoder.execution.cancellation import (
    CancellationRequested,
    CancellationSource,
    CancellationToken,
)
from truecoder.execution.clock import Clock, SystemClock, validate_clock
from truecoder.execution.errors import (
    AuditPersistenceError,
    AuditUnavailableError,
    BackendCleanupError,
    BackendOperationError,
    BackendSelectionError,
    BackendStartError,
    ExecutionInfrastructureError,
    InvalidExecutionStateError,
)
from truecoder.execution.events import (
    DEFAULT_EVENT_CAPACITY,
    ExecutionEventSink,
    LifecyclePublisher,
    NullEventSink,
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
    NativeDiagnostic,
    PolicyDecision,
    TerminationReason,
)
from truecoder.execution.output import CollectedOutput, OutputCollector
from truecoder.execution.preparation import PreparedExecution
from truecoder.execution.registry import ActiveExecution, ExecutionRegistry
from truecoder.execution.results import (
    CANCELLED_BEFORE_START,
    TERMINAL_STAGE_BY_STATUS,
    TerminalMaterial,
    build_cancelled_before_start_result,
    build_execution_result,
    build_finalization,
    build_output_evidence,
    cancellation_reason,
    claim_for_cancellation,
    claim_for_exit,
    claim_for_output_limit,
    claim_for_timeout,
    empty_output,
    public_status,
)

ApprovalGate: TypeAlias = Callable[
    [PreparedExecution, PolicyDecision, ExecutionContext],
    Awaitable[bool],
]

DEFAULT_SAFETY_DEADLINE_SECONDS: Final = 2.0

_EVENT_BY_SOURCE: Final[dict[str, AuditEventType]] = {
    "output_limit": AuditEventType.LIMIT_REACHED,
    "resource_limit": AuditEventType.LIMIT_REACHED,
    "cancellation": AuditEventType.CANCELLATION_REQUESTED,
    "timeout": AuditEventType.TIMEOUT_REACHED,
}


@runtime_checkable
class PreviewSink(Protocol):
    async def publish_bounded(
        self,
        execution_id: str,
        stream: str,
        text: str,
    ) -> None: ...


class NullPreviewSink:
    async def publish_bounded(
        self,
        execution_id: str,
        stream: str,
        text: str,
    ) -> None:
        del execution_id, stream, text


class _EvidenceLost(Exception):
    def __init__(self, boundary: str, cause: BaseException) -> None:
        self.boundary = boundary
        self.cause = cause
        super().__init__(boundary)


class _OutputPumpFailed(Exception):
    pass


class ExecutionRunner:
    def __init__(
        self,
        audit: AuditService,
        backends: BackendRegistry,
        *,
        registry: ExecutionRegistry | None = None,
        approval_gate: ApprovalGate | None = None,
        clock: Clock | None = None,
        event_sink: ExecutionEventSink | None = None,
        preview_sink: PreviewSink | None = None,
        event_capacity: int = DEFAULT_EVENT_CAPACITY,
        safety_deadline_seconds: float = DEFAULT_SAFETY_DEADLINE_SECONDS,
    ) -> None:
        if not isinstance(audit, AuditService):
            raise TypeError("audit must be an AuditService")
        if not isinstance(backends, BackendRegistry):
            raise TypeError("backends must be a BackendRegistry")
        if approval_gate is not None and not callable(approval_gate):
            raise TypeError("approval_gate must be callable")
        if isinstance(safety_deadline_seconds, bool) or not isinstance(
            safety_deadline_seconds,
            (int, float),
        ):
            raise TypeError("safety_deadline_seconds must be a number")
        if safety_deadline_seconds <= 0:
            raise ValueError("safety_deadline_seconds must be greater than zero")

        self._audit = audit
        self._backends = backends
        self._registry = registry or ExecutionRegistry()
        self._approval_gate = approval_gate
        self._clock = validate_clock(clock or SystemClock())
        self._event_sink = event_sink or NullEventSink()
        self._preview_sink = preview_sink
        self._event_capacity = event_capacity
        self._safety_deadline = float(safety_deadline_seconds)

    async def run_prepared(
        self,
        prepared: PreparedExecution,
        decision: PolicyDecision,
        context: ExecutionContext,
        *,
        cancellation_source: CancellationSource | None = None,
    ) -> ExecutionResult:
        if not isinstance(prepared, PreparedExecution):
            raise TypeError("prepared must be a PreparedExecution")
        if not isinstance(decision, PolicyDecision):
            raise TypeError("decision must be a PolicyDecision")
        if not isinstance(context, ExecutionContext):
            raise TypeError("context must be an ExecutionContext")
        if cancellation_source is not None and not isinstance(
            cancellation_source,
            CancellationSource,
        ):
            raise TypeError("cancellation_source must be a CancellationSource")

        publisher = self._publisher(context)
        state = LifecycleState(context.execution_id)
        try:
            await publisher.publish(
                "requested",
                details=_request_event_details(prepared.request),
            )
            return await self._run(
                prepared,
                decision,
                context,
                state,
                publisher,
                cancellation_source or CancellationSource(),
            )
        finally:
            await publisher.aclose()

    async def deny(
        self,
        request: ExecutionRequest,
        decision: PolicyDecision,
        context: ExecutionContext,
    ) -> ExecutionResult:
        if decision.allowed:
            raise ValueError("deny requires a policy decision that refused the request")

        publisher = self._publisher(context)
        state = LifecycleState(context.execution_id)
        try:
            await publisher.publish(
                "requested",
                details=_request_event_details(request),
            )
            handle = await self._admit(request, context)
            state.transition(RunState.POLICY_EVALUATED)
            await publisher.publish("policy_evaluated")
            return await self._deny(handle, state, context, decision, publisher)
        finally:
            await publisher.aclose()

    async def refuse(
        self,
        request: ExecutionRequest,
        context: ExecutionContext,
        *,
        detail: str,
        error: BaseException,
    ) -> ExecutionResult:
        publisher = self._publisher(context)
        state = LifecycleState(context.execution_id)
        try:
            await publisher.publish(
                "requested",
                details=_request_event_details(request),
            )
            handle = await self._admit(request, context)
            state.transition(RunState.POLICY_EVALUATED)
            await publisher.publish("policy_evaluated")
            return await self._fail_to_start(
                handle,
                state,
                context,
                None,
                publisher,
                detail=detail,
                error=error,
            )
        finally:
            await publisher.aclose()

    def _publisher(self, context: ExecutionContext) -> LifecyclePublisher:
        return LifecyclePublisher(
            context.execution_id,
            self._event_sink,
            self._clock,
            capacity=self._event_capacity,
        )

    async def _run(
        self,
        prepared: PreparedExecution,
        decision: PolicyDecision,
        context: ExecutionContext,
        state: LifecycleState,
        publisher: LifecyclePublisher,
        cancellation_source: CancellationSource,
    ) -> ExecutionResult:
        handle = await self._admit(prepared.request, context)
        entry = ActiveExecution(
            context=context,
            cancellation_source=cancellation_source,
            audit_handle=handle,
        )
        await self._registry.register(entry)
        token = entry.cancellation_source.token

        try:
            return await self._route(
                handle,
                token,
                prepared,
                decision,
                context,
                state,
                publisher,
            )
        finally:
            await self._registry.unregister(
                context.execution_id,
                expected=entry,
            )

    async def _route(
        self,
        handle: AuditRunHandle,
        token: CancellationToken,
        prepared: PreparedExecution,
        decision: PolicyDecision,
        context: ExecutionContext,
        state: LifecycleState,
        publisher: LifecyclePublisher,
    ) -> ExecutionResult:
        state.transition(RunState.POLICY_EVALUATED)
        await publisher.publish("policy_evaluated")
        if not decision.allowed:
            return await self._deny(handle, state, context, decision, publisher)

        await self._write_pre_start_event(
            handle,
            state,
            context,
            AuditEventType.POLICY_ALLOWED,
            publisher,
        )
        if token.cancelled:
            return await self._cancel_before_start(
                handle,
                state,
                context,
                prepared,
                token,
                publisher,
            )

        state.transition(RunState.PREPARED)
        await publisher.publish(
            "backend_selected",
            details=(("backend", prepared.backend.name),),
        )

        approved = await self._approve(
            handle,
            state,
            prepared,
            decision,
            context,
            publisher,
        )
        if token.cancelled:
            return await self._cancel_before_start(
                handle,
                state,
                context,
                prepared,
                token,
                publisher,
            )
        if not approved:
            return await self._reject(handle, state, context, publisher)

        return await self._start_and_supervise(
            handle,
            token,
            state,
            prepared,
            context,
            publisher,
        )

    async def _cancel_before_start(
        self,
        handle: AuditRunHandle,
        state: LifecycleState,
        context: ExecutionContext,
        prepared: PreparedExecution,
        token: CancellationToken,
        publisher: LifecyclePublisher,
        *,
        resource: BackendResourceIdentifier | None = None,
    ) -> ExecutionResult:
        if state.current is not RunState.FINALIZING:
            state.transition(RunState.FINALIZING)
        material = self._pre_start_material("failed_to_start", "failed_to_start")
        record = await self._settle(
            handle,
            material,
            context,
            resource=resource,
            detail=CANCELLED_BEFORE_START,
        )
        state.transition(RunState.TERMINAL)
        result = build_cancelled_before_start_result(
            record,
            material,
            backend=prepared.backend.name,
            reason=cancellation_reason(token.reason or "user"),
        )
        await publisher.publish("cancelled")
        return result

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
        publisher: LifecyclePublisher,
    ) -> ExecutionResult:
        await self._write_pre_start_event(
            handle,
            state,
            context,
            AuditEventType.POLICY_DENIED,
            publisher,
            message=_first_reason(decision),
        )
        state.transition(RunState.FINALIZING)
        material = self._pre_start_material("denied", "policy_denied")
        record = await self._settle(
            handle,
            material,
            context,
            detail=_bounded_diagnostic(
                "policy_denied",
                _first_reason(decision),
            ),
        )
        state.transition(RunState.TERMINAL)
        return await self._publish_result(record, material, None, publisher)

    async def _approve(
        self,
        handle: AuditRunHandle,
        state: LifecycleState,
        prepared: PreparedExecution,
        decision: PolicyDecision,
        context: ExecutionContext,
        publisher: LifecyclePublisher,
    ) -> bool:
        if not decision.requires_approval:
            return True
        if self._approval_gate is None:
            return False

        state.transition(RunState.AWAITING_APPROVAL)
        await publisher.publish("approval_required")
        await self._write_pre_start_event(
            handle,
            state,
            context,
            AuditEventType.APPROVAL_REQUESTED,
            publisher,
        )
        approved = bool(await self._approval_gate(prepared, decision, context))
        await self._write_pre_start_event(
            handle,
            state,
            context,
            AuditEventType.APPROVAL_GRANTED
            if approved
            else AuditEventType.APPROVAL_REJECTED,
            publisher,
        )
        if approved:
            await publisher.publish("approved")
        return approved

    async def _reject(
        self,
        handle: AuditRunHandle,
        state: LifecycleState,
        context: ExecutionContext,
        publisher: LifecyclePublisher,
    ) -> ExecutionResult:
        state.transition(RunState.FINALIZING)
        material = self._pre_start_material("denied", "approval_rejected")
        record = await self._settle(
            handle,
            material,
            context,
            outcome_override=TerminalOutcome.APPROVAL_REJECTED,
            detail=_bounded_diagnostic(
                "approval_rejected",
                "The user rejected this execution request.",
            ),
        )
        state.transition(RunState.TERMINAL)
        return await self._publish_result(record, material, None, publisher)

    async def _start_and_supervise(
        self,
        handle: AuditRunHandle,
        token: CancellationToken,
        state: LifecycleState,
        prepared: PreparedExecution,
        context: ExecutionContext,
        publisher: LifecyclePublisher,
    ) -> ExecutionResult:
        state.transition(RunState.REGISTERED)

        try:
            await self._audit.append_event(
                handle,
                AuditEventType.BACKEND_STARTING,
            )
        except Exception as error:  # noqa: BLE001
            return await self._fail_to_start(
                handle,
                state,
                context,
                prepared.backend.name,
                publisher,
                detail="starting_event_unavailable",
                error=error,
            )

        if token.cancelled:
            return await self._cancel_before_start(
                handle,
                state,
                context,
                prepared,
                token,
                publisher,
            )

        state.transition(RunState.STARTING)
        await publisher.publish("starting")
        return await self._supervise(
            handle,
            state,
            prepared,
            context,
            token,
            publisher,
        )

    async def _supervise(
        self,
        handle: AuditRunHandle,
        state: LifecycleState,
        prepared: PreparedExecution,
        context: ExecutionContext,
        cancellation: CancellationToken,
        publisher: LifecyclePublisher,
    ) -> ExecutionResult:
        backend = self._backends.get_exact(
            prepared.backend,
            execution_id=context.execution_id,
        )
        attached: list[BackendResourceIdentifier] = []

        async def attach(resource: BackendResourceIdentifier) -> None:
            await self._audit.attach_resource(handle, resource)
            attached.append(resource)

        try:
            execution = await backend.start(
                prepared,
                prepared.request,
                BackendStartContext(
                    execution=context,
                    audit_run_id=handle.run_id,
                ),
                cancellation,
                attach,
            )
        except CancellationRequested:
            return await self._cancel_before_start(
                handle,
                state,
                context,
                prepared,
                cancellation,
                publisher,
                resource=attached[0] if attached else None,
            )
        except Exception as error:  # noqa: BLE001
            return await self._fail_to_start(
                handle,
                state,
                context,
                prepared.backend.name,
                publisher,
                detail="backend_start_failed",
                error=error,
                resource=attached[0] if attached else None,
                reraise=not isinstance(error, BackendStartError),
            )

        started_at = self._clock.monotonic()
        resource = execution.resource

        try:
            await self._audit.mark_running(handle, resource)
        except Exception as error:
            await _terminate_quietly(execution, "shutdown")
            await _cleanup_quietly(execution)
            raise AuditPersistenceError(
                "execution was stopped because running evidence could not be stored",
                execution_id=context.execution_id,
                backend=prepared.backend.name,
                operation="mark_running",
            ) from error

        state.transition(RunState.RUNNING)
        await publisher.publish("started")
        return await self._await_terminal(
            handle,
            state,
            prepared,
            context,
            execution,
            cancellation,
            resource,
            started_at,
            publisher,
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
        publisher: LifecyclePublisher,
    ) -> ExecutionResult:
        limits = prepared.request.limits
        collector = OutputCollector(
            limits,
            redaction_values=prepared.environment.redaction_values,
        )
        limit_event = asyncio.Event()
        pump_failed = asyncio.Event()
        arbiter = TerminalArbiter()

        output_task = asyncio.create_task(
            self._pump(execution, collector, limit_event, pump_failed),
        )
        wait_task = asyncio.create_task(execution.wait())
        cancel_task = asyncio.create_task(cancellation.wait())
        timeout_task = asyncio.create_task(
            self._clock.sleep(limits.timeout_seconds),
        )
        output_limit_task = asyncio.create_task(limit_event.wait())
        pump_failed_task = asyncio.create_task(pump_failed.wait())
        watchers = {
            wait_task,
            cancel_task,
            timeout_task,
            output_limit_task,
            pump_failed_task,
        }
        created = watchers | {output_task}

        try:
            done, _pending = await asyncio.wait(
                watchers,
                return_when=asyncio.FIRST_COMPLETED,
            )
            observed_at = self._clock.monotonic()
            backend_exit_signal = _task_result(wait_task) if wait_task in done else None
            wait_failed = wait_task in done and backend_exit_signal is None
            candidate = choose_terminal_candidate(
                done=done,
                backend_exit=backend_exit_signal,
                cancellation=cancellation.reason if cancel_task in done else None,
                output_limit=output_limit_task in done,
                timeout=timeout_task in done,
                pump_failed=pump_failed_task in done or wait_failed,
                observed_at=observed_at,
            )
            claim = (await arbiter.claim(candidate)).claim

            for task in (cancel_task, timeout_task, output_limit_task):
                if task is not wait_task:
                    task.cancel()

            if claim.source != "backend_exit":
                _enter_terminating(state)
                await publisher.publish(
                    "terminating",
                    details=(("reason", claim.reason or "cancellation"),),
                )
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

            reap_error: BackendOperationError | None = None
            try:
                backend_exit = await self._reap(wait_task, context, prepared)
            except BackendOperationError as error:
                backend_exit = None
                reap_error = error

            output_complete = await self._await_output_eof(output_task)
            cleanup = await _finish_cleanup(execution)
            material = self._material(
                claim,
                backend_exit,
                collector.snapshot(),
                cleanup,
                started_at,
                output_complete=output_complete,
            )
        except _EvidenceLost as loss:
            material = await self._salvage(
                arbiter,
                execution,
                collector,
                output_task,
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
            for task in created:
                task.cancel()
            await asyncio.gather(*created, return_exceptions=True)

        state.transition(RunState.FINALIZING)
        record = await self._settle(
            handle,
            material,
            context,
            resource=resource,
        )
        state.transition(RunState.TERMINAL)

        if reap_error is not None:
            await publisher.publish(
                TERMINAL_STAGE_BY_STATUS[public_status(_finalization(record))],
                message="the backend could not be reaped",
            )
            raise reap_error

        if material.cleanup_incomplete:
            await publisher.publish(
                TERMINAL_STAGE_BY_STATUS[public_status(_finalization(record))],
                message="cleanup did not complete",
            )
            raise BackendCleanupError(
                "the backend could not release execution resources",
                execution_id=context.execution_id,
                backend=prepared.backend.name,
                operation="cleanup",
                diagnostic=material.cleanup.diagnostic if material.cleanup else None,
            )

        return await self._publish_result(
            record,
            material,
            prepared.backend.name,
            publisher,
        )

    async def _pump(
        self,
        execution: ExecutionHandle,
        collector: OutputCollector,
        limit_event: asyncio.Event,
        pump_failed: asyncio.Event,
    ) -> None:
        closed = False
        try:
            async for chunk in execution.output():
                if chunk.stream == "stdout":
                    update = collector.feed_stdout(chunk.data)
                else:
                    update = collector.feed_stderr(chunk.data)

                if update.newly_exceeded:
                    limit_event.set()

                if self._preview_sink is not None and update.text:
                    await self._preview_sink.publish_bounded(
                        execution.execution_id,
                        chunk.stream,
                        update.text,
                    )

            closed = True
            self._close_streams(collector)
        except asyncio.CancelledError:
            if not closed:
                self._close_streams(collector)
            raise
        except Exception as error:
            if not closed:
                self._close_streams(collector)
            pump_failed.set()
            raise _OutputPumpFailed(str(error)) from error

    @staticmethod
    def _close_streams(collector: OutputCollector) -> None:
        for close in (collector.close_stdout, collector.close_stderr):
            try:
                close()
            except RuntimeError:
                pass

    async def _await_output_eof(self, output_task: asyncio.Task) -> bool:
        try:
            await asyncio.wait_for(
                asyncio.shield(output_task),
                timeout=self._safety_deadline,
            )
            return True
        except TimeoutError:
            return False
        except _OutputPumpFailed:
            return False
        except Exception:  # noqa: BLE001
            return False

    async def _reap(
        self,
        wait_task: asyncio.Task,
        context: ExecutionContext,
        prepared: PreparedExecution,
    ) -> BackendExit | None:
        try:
            return await asyncio.wait_for(
                asyncio.shield(wait_task),
                timeout=self._safety_deadline,
            )
        except TimeoutError as error:
            raise BackendOperationError(
                "the backend did not finish terminating within the safety deadline",
                execution_id=context.execution_id,
                backend=prepared.backend.name,
                operation="reap",
            ) from error
        except Exception:  # noqa: BLE001
            return None

    def _candidate_material(
        self,
        claim: TerminalClaim,
        collector: OutputCollector,
        started_at: float,
        *,
        output_complete: bool,
    ) -> TerminalMaterial:
        return self._material(
            claim,
            None,
            collector.snapshot(),
            None,
            started_at,
            output_complete=output_complete,
        )

    async def _salvage(
        self,
        arbiter: TerminalArbiter,
        execution: ExecutionHandle,
        collector: OutputCollector,
        output_task: asyncio.Task,
        started_at: float,
    ) -> TerminalMaterial:
        outcome = await arbiter.claim(
            TerminalClaim(
                status="cancelled",
                reason="shutdown",
                observed_at_monotonic=self._clock.monotonic(),
                source="cancellation",
            )
        )
        await _terminate_quietly(execution, "shutdown")
        output_complete = await self._await_output_eof(output_task)
        cleanup = await _cleanup_quietly(execution)
        return self._material(
            outcome.claim,
            None,
            collector.snapshot(),
            cleanup,
            started_at,
            output_complete=output_complete,
        )

    async def _fail_to_start(
        self,
        handle: AuditRunHandle,
        state: LifecycleState,
        context: ExecutionContext,
        backend: BackendName | None,
        publisher: LifecyclePublisher,
        *,
        detail: str,
        error: BaseException,
        resource: BackendResourceIdentifier | None = None,
        reraise: bool = False,
        raise_original: bool = False,
    ) -> ExecutionResult:
        public_detail = _public_failure_detail(detail, error)
        state.transition(RunState.FINALIZING)
        material = self._pre_start_material("failed_to_start", "failed_to_start")
        record = await self._try_settle(
            handle,
            material,
            context,
            resource=resource,
            detail=public_detail,
        )
        state.transition(RunState.TERMINAL)
        await publisher.publish("failed_to_start", message=public_detail)

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

    async def _publish_result(
        self,
        record: AuditRunRecord,
        material: TerminalMaterial,
        backend: BackendName | None,
        publisher: LifecyclePublisher,
    ) -> ExecutionResult:
        result = build_execution_result(record, material, backend=backend)
        await publisher.publish(TERMINAL_STAGE_BY_STATUS[result.status])
        return result

    async def _write_pre_start_event(
        self,
        handle: AuditRunHandle,
        state: LifecycleState,
        context: ExecutionContext,
        event_type: AuditEventType,
        publisher: LifecyclePublisher,
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
            await publisher.publish("failed_to_start")
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
        observed_at = self._clock.monotonic()
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
        *,
        output_complete: bool = True,
    ) -> TerminalMaterial:
        complete = output.complete and output_complete
        return TerminalMaterial(
            claim=claim,
            backend_exit=backend_exit,
            output=output,
            audit_output=build_output_evidence(output, complete=complete),
            cleanup=cleanup,
            started_at_monotonic=started_at,
            finished_at_monotonic=self._clock.monotonic(),
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
            outcome_override=outcome_override,
        )
        try:
            return await self._audit.finalize(handle, finalization)
        except Exception as error:
            raise AuditPersistenceError(
                "the execution result was withheld because its terminal "
                "evidence could not be stored",
                execution_id=context.execution_id,
                operation="finalize",
            ) from error


def choose_terminal_candidate(
    *,
    done: set[asyncio.Task],
    backend_exit: BackendExit | None,
    cancellation: str | None,
    output_limit: bool,
    timeout: bool,
    observed_at: float,
    pump_failed: bool = False,
) -> TerminalClaim:
    candidates: list[TerminalClaim] = []

    if backend_exit is not None:
        candidates.append(claim_for_exit(backend_exit, observed_at))
    if output_limit:
        candidates.append(claim_for_output_limit(observed_at))
    if cancellation is not None:
        candidates.append(claim_for_cancellation(cancellation, observed_at))
    if pump_failed:
        candidates.append(claim_for_cancellation("shutdown", observed_at))
    if timeout:
        candidates.append(claim_for_timeout(observed_at))

    if not candidates:
        raise InvalidExecutionStateError(
            f"the terminal wait returned no rankable signal from {len(done)} tasks",
            operation="await_terminal",
        )
    return resolve_terminal_claim(tuple(candidates))


def _public_failure_detail(code: str, error: BaseException) -> str:
    messages = {
        "backend_unavailable": (
            "No enabled execution backend satisfies the requested capabilities."
        ),
        "container_network_unconfigured": (
            "Sandbox network access is not configured. Retry without network access."
        ),
        "backend_start_failed": "The selected backend could not start the command.",
        "starting_event_unavailable": (
            "The required audit start event could not be stored."
        ),
        "pre_start_event_unavailable": (
            "Required pre-execution evidence could not be stored."
        ),
        "runtime_evidence_lost": (
            "The backend started but its durable resource evidence was lost."
        ),
    }
    message = messages.get(code, "The execution could not be started safely.")
    if isinstance(error, (BackendSelectionError, BackendStartError)):
        message = error.message
    elif isinstance(error, ExecutionInfrastructureError):
        message = messages.get(code, message)
    return _bounded_diagnostic(code, message)


def _request_event_details(
    request: ExecutionRequest,
) -> tuple[tuple[str, str], ...]:
    if request.mode == "shell":
        command = request.script or ""
    else:
        command = shlex.join(request.argv or ())
    if len(command) > 500:
        command = f"{command[:499]}…"
    return (("command", command),)


def _bounded_diagnostic(code: str, message: str | None) -> str:
    normalized_code = "".join(
        character
        for character in code.strip().replace(" ", "_")
        if character.isalnum() or character in "._-"
    )[:128]
    if not normalized_code:
        normalized_code = "execution_failure"
    if not message:
        return normalized_code
    normalized_message = " ".join(message.split())
    budget = 512 - len(normalized_code) - 2
    return f"{normalized_code}: {normalized_message[:budget]}"


def _enter_terminating(state: LifecycleState) -> None:
    if state.current is RunState.RUNNING:
        state.transition(RunState.TERMINATING)


def _finalization(record: AuditRunRecord):
    assert record.finalization is not None
    return record.finalization


def _task_result(task: asyncio.Task) -> BackendExit | None:
    if task.cancelled() or task.exception() is not None:
        return None
    return task.result()


def _first_reason(decision: PolicyDecision) -> str | None:
    for reason in decision.reasons:
        return reason.message
    return None


async def _terminate_quietly(
    execution: ExecutionHandle,
    reason: TerminationReason,
    grace_seconds: float = 0.0,
) -> None:
    with suppress(Exception):
        await execution.terminate(reason, grace_seconds)


async def _cleanup_quietly(execution: ExecutionHandle) -> CleanupResult | None:
    try:
        return await execution.cleanup()
    except Exception:  # noqa: BLE001
        return None


async def _finish_cleanup(execution: ExecutionHandle) -> CleanupResult:
    try:
        return await execution.cleanup()
    except Exception as error:  # noqa: BLE001
        return CleanupResult(
            complete=False,
            diagnostic=NativeDiagnostic(
                code="cleanup-raised",
                message=str(error)[:4096] or "cleanup raised",
                platform="posix",
            ),
        )
