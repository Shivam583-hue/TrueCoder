from __future__ import annotations

import asyncio
import os
import platform
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from ..audit.models import BackendResourceIdentifier
from ..cancellation import CancellationToken
from ..errors import BackendStartError
from ..models import ExecutionRequest, TerminationReason
from ..preparation import PreparedExecution
from .base import BackendResourceRegistrar, BackendStartContext
from .models import (
    BackendDescriptor,
    BackendExit,
    BackendOutputChunk,
    CleanupResult,
    DiscoveredProgram,
    DiscoverySnapshot,
    NativeDiagnostic,
    OutputStreamName,
)
from .windows_native import (
    WINDOWS,
    NativeProcess,
    WindowsNativeError,
    active_process_count,
    close_handle,
    create_suspended,
    exit_code,
    read_pipe,
    resume,
    terminate_job,
    wait_process,
)
from .windows_plan import WindowsLaunchPlan, build_windows_plan

WINDOWS_RESOURCE_VERSION = 1
WINDOWS_RESOURCE_KIND = "job-object"
_POLL_SECONDS = 0.05


def current_windows_host_id() -> str:
    return f"{platform.node() or 'windows'}:{os.getpid()}"


class WindowsExecutionHandle:
    def __init__(
        self,
        execution_id: str,
        process: NativeProcess,
        resource: BackendResourceIdentifier,
    ) -> None:
        self._execution_id = execution_id
        self._process = process
        self._resource = resource
        self._output_claimed = False
        self._wait_task: asyncio.Task[BackendExit] | None = None
        self._terminate_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[CleanupResult] | None = None
        self._termination_reason: TerminationReason | None = None

    @property
    def execution_id(self) -> str:
        return self._execution_id

    @property
    def resource(self) -> BackendResourceIdentifier:
        return self._resource

    def output(self) -> AsyncIterator[BackendOutputChunk]:
        if self._output_claimed:
            raise RuntimeError("the output iterator already has an owner")
        self._output_claimed = True
        return self._drain()

    async def _drain(self) -> AsyncIterator[BackendOutputChunk]:
        loop = asyncio.get_running_loop()
        streams: tuple[tuple[OutputStreamName, int], ...] = (
            ("stdout", self._process.stdout_read),
            ("stderr", self._process.stderr_read),
        )
        while True:
            produced = False
            for name, handle in streams:
                data = await loop.run_in_executor(None, read_pipe, handle)
                if data:
                    produced = True
                    yield BackendOutputChunk(stream=name, data=data)
            if produced:
                continue
            if exit_code(self._process.process_handle) is not None:
                for name, handle in streams:
                    data = await loop.run_in_executor(None, read_pipe, handle)
                    if data:
                        yield BackendOutputChunk(stream=name, data=data)
                return
            await asyncio.sleep(_POLL_SECONDS)

    async def wait(self) -> BackendExit:
        if self._wait_task is None:
            self._wait_task = asyncio.create_task(self._wait_once())
        return await asyncio.shield(self._wait_task)

    async def _wait_once(self) -> BackendExit:
        loop = asyncio.get_running_loop()
        while True:
            code = exit_code(self._process.process_handle)
            if code is not None:
                return BackendExit(
                    exit_code=code,
                    native_reason=self._termination_reason,
                )
            await loop.run_in_executor(
                None,
                wait_process,
                self._process.process_handle,
                int(_POLL_SECONDS * 1000),
            )

    async def terminate(
        self,
        reason: TerminationReason,
        grace_seconds: float,
    ) -> None:
        if self._terminate_task is None:
            self._termination_reason = reason
            self._terminate_task = asyncio.create_task(
                self._terminate_once(grace_seconds)
            )
        await asyncio.shield(self._terminate_task)

    async def _terminate_once(self, grace_seconds: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, grace_seconds)
        while loop.time() < deadline:
            if exit_code(self._process.process_handle) is not None:
                return
            await asyncio.sleep(_POLL_SECONDS)
        terminate_job(self._process.job_handle)

    async def cleanup(self) -> CleanupResult:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_once())
        return await asyncio.shield(self._cleanup_task)

    async def _cleanup_once(self) -> CleanupResult:
        problems: list[str] = []
        try:
            terminate_job(self._process.job_handle)
        except WindowsNativeError as error:
            problems.append(f"job termination failed: {error.error_code}")
        for handle in (
            self._process.stdout_read,
            self._process.stderr_read,
            self._process.thread_handle,
            self._process.process_handle,
            self._process.job_handle,
        ):
            try:
                close_handle(handle)
            except WindowsNativeError as error:
                problems.append(f"handle close failed: {error.error_code}")
        if problems:
            return CleanupResult(
                complete=False,
                diagnostic=NativeDiagnostic(
                    code="windows-cleanup-incomplete",
                    message="; ".join(problems)[:4096],
                    platform="windows",
                ),
            )
        return CleanupResult(complete=True)


class WindowsBackend:
    def __init__(
        self,
        descriptor: BackendDescriptor,
        *,
        shells: tuple[DiscoveredProgram, ...] = (),
        host_id: str | None = None,
    ) -> None:
        if not isinstance(descriptor, BackendDescriptor):
            raise TypeError("descriptor must be a BackendDescriptor")
        if descriptor.name != "windows":
            raise ValueError("descriptor must describe the windows backend")
        self._descriptor = descriptor
        self._shells = shells
        self._host_id = host_id or current_windows_host_id()

    @classmethod
    def from_snapshot(cls, snapshot: DiscoverySnapshot) -> WindowsBackend:
        if not isinstance(snapshot, DiscoverySnapshot):
            raise TypeError("snapshot must be a DiscoverySnapshot")
        return cls(snapshot.backend("windows"), shells=snapshot.shells)

    @property
    def descriptor(self) -> BackendDescriptor:
        return self._descriptor

    async def start(
        self,
        prepared: PreparedExecution,
        request: ExecutionRequest,
        context: BackendStartContext,
        cancellation: CancellationToken,
        register_resource: BackendResourceRegistrar,
    ) -> WindowsExecutionHandle:
        del request
        if not WINDOWS:
            raise BackendStartError(
                "the windows backend requires a windows host",
                execution_id=context.execution_id,
                backend="windows",
                operation="start",
            )

        plan = self._plan(prepared, context)
        ownership_token = uuid.uuid4().hex

        try:
            process = create_suspended(plan)
        except WindowsNativeError as error:
            raise BackendStartError(
                f"windows process creation failed: {error.reason}",
                execution_id=context.execution_id,
                backend="windows",
                operation="start",
            ) from error

        resource = BackendResourceIdentifier(
            version=WINDOWS_RESOURCE_VERSION,
            backend="windows",
            resource_kind=WINDOWS_RESOURCE_KIND,
            resource_id=str(process.process_id),
            ownership_token=ownership_token,
            host_id=self._host_id,
            created_at_utc=datetime.now(UTC),
            native_details=(
                ("audit_run_id", context.audit_run_id),
                ("job_handle", str(process.job_handle)),
                ("process_id", str(process.process_id)),
            ),
        )

        try:
            await register_resource(resource)
        except BaseException:
            self._discard(process)
            raise

        if cancellation.cancelled:
            self._discard(process)
            raise BackendStartError(
                "cancelled before the windows job was released",
                execution_id=context.execution_id,
                backend="windows",
                operation="start",
            )

        try:
            resume(process)
        except WindowsNativeError as error:
            self._discard(process)
            raise BackendStartError(
                f"windows thread resume failed: {error.reason}",
                execution_id=context.execution_id,
                backend="windows",
                operation="start",
            ) from error

        return WindowsExecutionHandle(context.execution_id, process, resource)

    def _plan(
        self,
        prepared: PreparedExecution,
        context: BackendStartContext,
    ) -> WindowsLaunchPlan:
        try:
            return build_windows_plan(prepared, self._shells)
        except (TypeError, ValueError) as error:
            raise BackendStartError(
                str(error),
                execution_id=context.execution_id,
                backend="windows",
                operation="plan",
            ) from error

    @staticmethod
    def _discard(process: NativeProcess) -> None:
        try:
            terminate_job(process.job_handle)
        except WindowsNativeError:
            pass
        for handle in (
            process.stdout_read,
            process.stderr_read,
            process.thread_handle,
            process.process_handle,
            process.job_handle,
        ):
            try:
                close_handle(handle)
            except WindowsNativeError:
                pass


class WindowsRecoveryHandler:
    def __init__(self, host_id: str | None = None) -> None:
        self._host_id = host_id or current_windows_host_id()

    async def recover(self, resource: BackendResourceIdentifier) -> str:
        if not isinstance(resource, BackendResourceIdentifier):
            raise TypeError("resource must be a BackendResourceIdentifier")
        if resource.backend != "windows":
            return "recovery_failed"
        if resource.resource_kind != WINDOWS_RESOURCE_KIND:
            return "recovery_failed"
        if resource.host_id != self._host_id:
            return "recovery_failed"
        if resource.version != WINDOWS_RESOURCE_VERSION:
            return "recovery_failed"
        if not WINDOWS:
            return "recovery_failed"

        details = dict(resource.native_details)
        raw_job = details.get("job_handle")
        if raw_job is None or not raw_job.isdigit():
            return "recovery_failed"

        job_handle = int(raw_job)
        try:
            if active_process_count(job_handle) == 0:
                close_handle(job_handle)
                return "absent"
            terminate_job(job_handle)
            close_handle(job_handle)
        except WindowsNativeError:
            return "recovery_failed"
        return "terminated"
