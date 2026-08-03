from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from truecoder.execution.audit.models import (
    AuditEvent,
    AuditEventType,
    AuditFinalization,
    AuditRunAdmission,
    AuditRunHandle,
    AuditRunPhase,
    AuditRunRecord,
    AuditRunSnapshot,
    AuditRunStart,
    BackendResourceIdentifier,
)
from truecoder.execution.backends.base import BackendStartContext
from truecoder.execution.backends.models import (
    BackendDescriptor,
    BackendExit,
    BackendOutputChunk,
    CleanupResult,
)
from truecoder.execution.models import (
    ExecutionLifecycleEvent,
    NativeDiagnostic,
)

EPOCH = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class FakeClockError(RuntimeError):
    pass


class FakeClock:
    def __init__(
        self,
        *,
        start_utc: datetime = EPOCH,
        start_monotonic: float = 1000.0,
    ) -> None:
        self._utc = start_utc
        self._monotonic = start_monotonic
        self._sleepers: list[tuple[float, int, asyncio.Future]] = []
        self._order = itertools.count()

    def now_utc(self) -> datetime:
        return self._utc

    def monotonic(self) -> float:
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise FakeClockError("cannot sleep for a negative duration")
        if seconds == 0:
            await asyncio.sleep(0)
            return

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        heapq.heappush(
            self._sleepers,
            (self._monotonic + seconds, next(self._order), future),
        )
        await future

    @property
    def pending_sleepers(self) -> int:
        return len(self._sleepers)

    async def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise FakeClockError("cannot advance time backwards")

        target = self._monotonic + seconds
        while self._sleepers and self._sleepers[0][0] <= target:
            due, _order, future = heapq.heappop(self._sleepers)
            self._monotonic = due
            self._utc += timedelta(seconds=due - (target - seconds))
            if not future.done():
                future.set_result(None)
            await asyncio.sleep(0)

        self._monotonic = target
        self._utc = EPOCH + timedelta(seconds=target - 1000.0)
        await asyncio.sleep(0)

    def tick(self, seconds: float) -> None:
        if seconds < 0:
            raise FakeClockError("cannot advance time backwards")
        self._monotonic += seconds
        self._utc += timedelta(seconds=seconds)


class ScriptedHandle:
    def __init__(
        self,
        context: BackendStartContext,
        *,
        chunks: tuple[BackendOutputChunk, ...] = (),
        exit_code: int | None = 0,
        native_reason: str | None = None,
        cleanup_complete: bool = True,
        output_error: Exception | None = None,
        wait_error: Exception | None = None,
        cleanup_error: Exception | None = None,
        terminate_error: Exception | None = None,
        block_termination: bool = False,
        gate_exit: bool = False,
        gate_output: bool = False,
    ) -> None:
        self._context = context
        self._chunks = chunks
        self._exit_code = exit_code
        self._native_reason = native_reason
        self._cleanup_complete = cleanup_complete
        self._output_error = output_error
        self._wait_error = wait_error
        self._cleanup_error = cleanup_error
        self._terminate_error = terminate_error
        self._block_termination = block_termination

        self.output_claim_count = 0
        self.wait_count = 0
        self.terminate_count = 0
        self.cleanup_count = 0
        self.termination_reasons: list[str] = []

        self.output_allowed = asyncio.Event()
        self.wait_allowed_to_complete = asyncio.Event()
        self.terminate_entered = asyncio.Event()
        self.cleanup_entered = asyncio.Event()
        self.output_exhausted = asyncio.Event()
        if not gate_output:
            self.output_allowed.set()
        if not gate_exit:
            self.wait_allowed_to_complete.set()

        self._resource = BackendResourceIdentifier(
            version=1,
            backend="posix",
            resource_kind="process_group",
            resource_id=f"pgid-{context.execution_id}",
            ownership_token=f"token-{context.execution_id}",
            host_id="fake-host",
            created_at_utc=EPOCH,
            native_details=(("pgid", "1234"),),
        )

    @property
    def execution_id(self) -> str:
        return self._context.execution_id

    @property
    def resource(self) -> BackendResourceIdentifier:
        return self._resource

    def output(self) -> AsyncIterator[BackendOutputChunk]:
        self.output_claim_count += 1

        async def iterate() -> AsyncIterator[BackendOutputChunk]:
            await self.output_allowed.wait()
            for chunk in self._chunks:
                yield chunk
                await asyncio.sleep(0)
            if self._output_error is not None:
                raise self._output_error
            self.output_exhausted.set()

        return iterate()

    async def wait(self) -> BackendExit:
        self.wait_count += 1
        if self._wait_error is not None:
            raise self._wait_error
        if not self.wait_allowed_to_complete.is_set():
            await self.wait_allowed_to_complete.wait()
        if self.termination_reasons:
            return BackendExit(
                exit_code=None,
                native_reason=self.termination_reasons[0],
            )
        return BackendExit(
            exit_code=self._exit_code,
            native_reason=self._native_reason,
        )

    async def terminate(self, reason: str, grace_seconds: float) -> None:
        self.terminate_count += 1
        self.termination_reasons.append(reason)
        self.terminate_entered.set()
        if self._terminate_error is not None:
            raise self._terminate_error
        if self._block_termination:
            await asyncio.Event().wait()
        self.wait_allowed_to_complete.set()

    async def cleanup(self) -> CleanupResult:
        self.cleanup_count += 1
        self.cleanup_entered.set()
        if self._cleanup_error is not None:
            raise self._cleanup_error
        if self._cleanup_complete:
            return CleanupResult(complete=True)
        return CleanupResult(
            complete=False,
            diagnostic=NativeDiagnostic(
                code="cleanup-incomplete",
                message="the resource survived cleanup",
                platform="posix",
            ),
        )

    def complete(self, exit_code: int | None = None) -> None:
        if exit_code is not None:
            self._exit_code = exit_code
        self.wait_allowed_to_complete.set()


class ScriptedBackend:
    def __init__(
        self,
        descriptor: BackendDescriptor,
        *,
        handle_options: dict | None = None,
        fail_before_registration: Exception | None = None,
        fail_after_registration: Exception | None = None,
        gate_start: bool = False,
    ) -> None:
        self._descriptor = descriptor
        self._handle_options = handle_options or {}
        self._fail_before_registration = fail_before_registration
        self._fail_after_registration = fail_after_registration

        self.start_count = 0
        self.prepared_seen: list[object] = []
        self.handle: ScriptedHandle | None = None
        self.registrar_failed = False
        self.gate_closed_on_abort = True

        self.start_entered = asyncio.Event()
        self.registrar_entered = asyncio.Event()
        self.registrar_completed = asyncio.Event()
        self.start_allowed_to_return = asyncio.Event()
        self.started = asyncio.Event()
        self.target_gate_opened = False
        if not gate_start:
            self.start_allowed_to_return.set()

    @property
    def descriptor(self) -> BackendDescriptor:
        return self._descriptor

    def set_descriptor(self, descriptor: BackendDescriptor) -> None:
        self._descriptor = descriptor

    async def start(
        self,
        prepared,
        request,
        context,
        cancellation,
        register_resource,
    ):
        del request
        self.start_count += 1
        self.prepared_seen.append(prepared)
        self.start_entered.set()
        cancellation.raise_if_cancelled()

        if self._fail_before_registration is not None:
            raise self._fail_before_registration

        handle = ScriptedHandle(context, **self._handle_options)
        self.registrar_entered.set()
        try:
            await register_resource(handle.resource)
        except BaseException:
            self.registrar_failed = True
            await handle.cleanup()
            raise
        self.registrar_completed.set()

        if self._fail_after_registration is not None:
            await handle.cleanup()
            raise self._fail_after_registration

        await self.start_allowed_to_return.wait()
        cancellation.raise_if_cancelled()
        self.target_gate_opened = True
        self.handle = handle
        self.started.set()
        return handle


class AuditSpy:
    def __init__(self, *, run_id: str = "run_spy_01") -> None:
        self.run_id = run_id
        self.calls: list[str] = []
        self.fail_on: set[str] = set()
        self.phase = AuditRunPhase.PENDING
        self.finalization: AuditFinalization | None = None
        self.resource: BackendResourceIdentifier | None = None
        self.finalize_count = 0
        self._sequence = 0
        self._created_at = EPOCH
        self._admission: AuditRunAdmission | None = None
        self._start: AuditRunStart | None = None
        self.events: list[AuditEventType] = []

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail_on:
            raise RuntimeError(f"audit failure injected at {name}")

    def create_pending(self, admission: AuditRunAdmission) -> AuditRunHandle:
        self._guard("admit")
        self._admission = admission
        self._created_at = admission.created_at
        return AuditRunHandle(
            run_id=admission.run_id,
            execution_id=admission.execution_id,
        )

    def append_event(
        self,
        run_id: str,
        event_type: AuditEventType,
        **kwargs,
    ) -> AuditEvent:
        del kwargs
        self._guard(f"append_event:{event_type.value}")
        self.events.append(event_type)
        self._sequence += 1
        return AuditEvent(
            event_id=f"event_{self._sequence:04d}",
            run_id=run_id,
            sequence=self._sequence,
            occurred_at=EPOCH,
            phase=self.phase,
            event_type=event_type,
        )

    def attach_resource(
        self,
        run_id: str,
        resource: BackendResourceIdentifier,
        **kwargs,
    ) -> BackendResourceIdentifier:
        del run_id, kwargs
        self._guard("attach_resource")
        self.resource = resource
        return resource

    def mark_running(self, start: AuditRunStart) -> AuditRunRecord:
        self._guard("mark_running")
        self._start = start
        self.phase = AuditRunPhase.RUNNING
        return self._record()

    def finalize(self, finalization: AuditFinalization) -> AuditRunRecord:
        self._guard("finalize")
        self.finalize_count += 1
        self.finalization = finalization
        self.phase = AuditRunPhase.TERMINAL
        return self._record()

    def get_run(self, run_id: str) -> AuditRunSnapshot:
        del run_id
        assert self._admission is not None
        return AuditRunSnapshot(
            admission=self._admission,
            record=self._record(),
            resource=self.resource,
        )

    def get_events(self, run_id: str) -> tuple[AuditEvent, ...]:
        del run_id
        return ()

    def claim_nonterminal(self, owner: str, **kwargs) -> tuple[AuditRunSnapshot, ...]:
        del owner, kwargs
        return ()

    def event_names(self) -> tuple[str, ...]:
        return tuple(
            call.split(":", 1)[1]
            for call in self.calls
            if call.startswith("append_event:")
        )

    def _record(self) -> AuditRunRecord:
        stamps = [self._created_at]
        if self._start is not None:
            stamps.append(self._start.started_at)
        if self.finalization is not None:
            stamps.append(self.finalization.finalized_at)
        return AuditRunRecord(
            run_id=self.run_id,
            created_at=self._created_at,
            updated_at=max(stamps),
            phase=self.phase,
            start=self._start,
            finalization=self.finalization,
        )


class CollectingEventSink:
    def __init__(self, *, fail: bool = False, block: bool = False) -> None:
        self.events: list[ExecutionLifecycleEvent] = []
        self.fail = fail
        self._block = block
        self.released = asyncio.Event()
        self.max_buffered = 0
        if not block:
            self.released.set()

    async def publish(self, event: ExecutionLifecycleEvent) -> None:
        if self._block:
            await self.released.wait()
        if self.fail:
            raise RuntimeError("event sink failure injected")
        self.events.append(event)
        self.max_buffered = max(self.max_buffered, len(self.events))

    def release(self) -> None:
        self.released.set()

    def stages(self) -> tuple[str, ...]:
        return tuple(event.stage for event in self.events)


class FakeApproval:
    def __init__(
        self,
        *,
        approve: bool = True,
        gate: bool = False,
        error: Exception | None = None,
    ) -> None:
        self._approve = approve
        self._error = error
        self.requests: list[object] = []
        self.responses = 0
        self.requested = asyncio.Event()
        self.allowed = asyncio.Event()
        if not gate:
            self.allowed.set()

    async def __call__(self, prepared, decision, context) -> bool:
        del context
        self.requests.append((prepared, decision))
        self.requested.set()
        await self.allowed.wait()
        if self._error is not None:
            raise self._error
        self.responses += 1
        return self._approve

    def release(self, *, approve: bool | None = None) -> None:
        if approve is not None:
            self._approve = approve
        self.allowed.set()


class PreviewCollector:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def publish_bounded(self, text: str) -> None:
        self.texts.append(text)
