from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypeAlias

from truecoder.execution.models import ExecutionContext, ExecutionRequest
from truecoder.execution.serialization import serialize_execution_model

from .codec import sha256_text
from .models import (
    AuditEvent,
    AuditEventType,
    AuditFinalization,
    AuditRunAdmission,
    AuditRunHandle,
    AuditRunRecord,
    AuditRunSnapshot,
    AuditRunStart,
    BackendResourceIdentifier,
    Metadata,
)
from .retention import RetentionPolicy, RetentionReport

UtcClock: TypeAlias = Callable[[], datetime]
IdFactory: TypeAlias = Callable[[], str]


class AuditStore(Protocol):
    def create_pending(self, admission: AuditRunAdmission) -> AuditRunHandle: ...

    def append_event(
        self,
        run_id: str,
        event_type: AuditEventType,
        *,
        message: str | None = None,
        metadata: Metadata = (),
        occurred_at: datetime | None = None,
    ) -> AuditEvent: ...

    def attach_resource(
        self,
        run_id: str,
        resource: BackendResourceIdentifier,
        *,
        attached_at: datetime | None = None,
    ) -> BackendResourceIdentifier: ...

    def mark_running(self, start: AuditRunStart) -> AuditRunRecord: ...

    def finalize(self, finalization: AuditFinalization) -> AuditRunRecord: ...

    def get_run(self, run_id: str) -> AuditRunSnapshot: ...

    def get_events(self, run_id: str) -> tuple[AuditEvent, ...]: ...

    def list_runs(
        self,
        *,
        workspace_id: str | None = None,
        limit: int = 200,
    ) -> tuple[AuditRunSnapshot, ...]: ...

    def apply_retention(
        self,
        policy: RetentionPolicy,
    ) -> RetentionReport: ...

    def claim_nonterminal(
        self,
        owner: str,
        *,
        lease_seconds: float = 30.0,
        limit: int = 100,
    ) -> tuple[AuditRunSnapshot, ...]: ...


class AuditService:
    """Async control-plane boundary for mandatory durable audit writes."""

    def __init__(
        self,
        store: AuditStore,
        *,
        run_id_factory: IdFactory | None = None,
        clock: UtcClock | None = None,
    ) -> None:
        if run_id_factory is not None and not callable(run_id_factory):
            raise TypeError("run_id_factory must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._store = store
        self._run_id_factory = run_id_factory or (lambda: f"run_{uuid.uuid4().hex}")
        self._clock = clock or (lambda: datetime.now(UTC))

    async def admit(
        self,
        context: ExecutionContext,
        request: ExecutionRequest,
    ) -> AuditRunHandle:
        """Persist pending evidence; callers must not execute without the handle."""

        if not isinstance(context, ExecutionContext):
            raise TypeError("context must be an ExecutionContext")
        if not isinstance(request, ExecutionRequest):
            raise TypeError("request must be an ExecutionRequest")
        request_payload = serialize_execution_model(request)
        admission = AuditRunAdmission(
            run_id=self._new_run_id(),
            execution_id=context.execution_id,
            tool_call_id=context.tool_call_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            workspace_id=context.workspace_id,
            request_sha256=sha256_text(request_payload),
            request_summary=_request_summary(request),
            created_at=self._now(),
        )
        # This await is the execution gate: a storage error propagates before
        # the caller receives any authority to start a backend.
        return await asyncio.to_thread(self._store.create_pending, admission)

    async def append_event(
        self,
        handle: AuditRunHandle,
        event_type: AuditEventType,
        *,
        message: str | None = None,
        metadata: Metadata = (),
    ) -> AuditEvent:
        _validate_handle(handle)
        await self._assert_handle_matches(handle)
        return await asyncio.to_thread(
            self._store.append_event,
            handle.run_id,
            event_type,
            message=message,
            metadata=metadata,
            occurred_at=self._now(),
        )

    async def list_runs(
        self,
        *,
        workspace_id: str | None = None,
        limit: int = 200,
    ) -> tuple[AuditRunSnapshot, ...]:
        return await asyncio.to_thread(
            self._store.list_runs,
            workspace_id=workspace_id,
            limit=limit,
        )

    async def apply_retention(
        self,
        policy: RetentionPolicy,
    ) -> RetentionReport:
        if not isinstance(policy, RetentionPolicy):
            raise TypeError("policy must be a RetentionPolicy")
        return await asyncio.to_thread(self._store.apply_retention, policy)

    async def attach_resource(
        self,
        handle: AuditRunHandle,
        resource: BackendResourceIdentifier,
    ) -> BackendResourceIdentifier:
        _validate_handle(handle)
        await self._assert_handle_matches(handle)
        return await asyncio.to_thread(
            self._store.attach_resource,
            handle.run_id,
            resource,
            attached_at=self._now(),
        )

    async def mark_running(
        self,
        handle: AuditRunHandle,
        resource: BackendResourceIdentifier,
        *,
        metadata: Metadata = (),
    ) -> AuditRunRecord:
        _validate_handle(handle)
        await self._assert_handle_matches(handle)
        start = AuditRunStart(
            run_id=handle.run_id,
            started_at=self._now(),
            resource=resource,
            metadata=metadata,
        )
        return await asyncio.to_thread(self._store.mark_running, start)

    async def finalize(
        self,
        handle: AuditRunHandle,
        finalization: AuditFinalization,
    ) -> AuditRunRecord:
        _validate_handle(handle)
        await self._assert_handle_matches(handle)
        if finalization.run_id != handle.run_id:
            raise ValueError("finalization run_id must match the audit handle")
        return await asyncio.to_thread(self._store.finalize, finalization)

    async def get_run(self, run_id: str) -> AuditRunSnapshot:
        return await asyncio.to_thread(self._store.get_run, run_id)

    async def get_events(self, run_id: str) -> tuple[AuditEvent, ...]:
        return await asyncio.to_thread(self._store.get_events, run_id)

    async def claim_nonterminal(
        self,
        owner: str,
        *,
        lease_seconds: float = 30.0,
        limit: int = 100,
    ) -> tuple[AuditRunSnapshot, ...]:
        return await asyncio.to_thread(
            self._store.claim_nonterminal,
            owner,
            lease_seconds=lease_seconds,
            limit=limit,
        )

    def now(self) -> datetime:
        return self._now()

    async def _assert_handle_matches(self, handle: AuditRunHandle) -> None:
        snapshot = await asyncio.to_thread(self._store.get_run, handle.run_id)
        if snapshot.admission.execution_id != handle.execution_id:
            raise ValueError("audit handle execution_id does not match its run")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("audit clock must return a UTC datetime")
        return value

    def _new_run_id(self) -> str:
        value = self._run_id_factory()
        if not isinstance(value, str) or not value.strip():
            raise ValueError("run_id_factory must return a non-empty string")
        return value


def _request_summary(request: ExecutionRequest) -> Metadata:
    if request.mode == "exec":
        assert request.argv is not None
        command = (
            subprocess.list2cmdline(request.argv)
            if os.name == "nt"
            else shlex.join(request.argv)
        )
    else:
        assert request.script is not None
        command = request.script

    limit = request.limits
    return (
        ("mode", request.mode),
        ("command", _bounded_value(command)),
        ("working_directory", _bounded_value(str(request.working_directory))),
        ("backend", request.backend),
        ("shell_kind", request.shell_kind),
        ("filesystem_mode", request.filesystem_mode),
        ("network_access", str(request.network_access).lower()),
        ("timeout_seconds", str(limit.timeout_seconds)),
        ("max_output_bytes", str(limit.max_output_bytes)),
        ("max_return_bytes", str(limit.max_return_bytes)),
        ("memory_bytes", _optional_number(limit.memory_bytes)),
        ("cpu_seconds", _optional_number(limit.cpu_seconds)),
        ("max_processes", _optional_number(limit.max_processes)),
        ("termination_grace_seconds", str(limit.termination_grace_seconds)),
        (
            "environment_names",
            _bounded_value(
                ",".join(sorted(name for name, _value in request.environment))
            ),
        ),
    )


def _optional_number(value: float | None) -> str:
    return "none" if value is None else str(value)


def _bounded_value(value: str, limit: int = 4096) -> str:
    if len(value) <= limit:
        return value
    marker = "...[truncated]"
    return value[: limit - len(marker)] + marker


def _validate_handle(handle: AuditRunHandle) -> None:
    if not isinstance(handle, AuditRunHandle):
        raise TypeError("handle must be an AuditRunHandle")
