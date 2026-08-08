from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Final

from ..audit.models import BackendResourceIdentifier
from ..cancellation import CancellationToken
from ..errors import BackendStartError
from ..models import (
    ExecutionRequest,
    NativeDiagnostic,
    TerminationReason,
)
from ..preparation import PreparedExecution
from .base import BackendResourceRegistrar, BackendStartContext
from .container_dialects import MAX_INSPECT_BYTES
from .container_identity import create_container_resource, verify_container_identity
from .container_models import ContainerCreatePlan, ContainerInspection
from .container_plan import (
    ContainerLaunchConfig,
    build_container_plan,
    build_env_file_content,
)
from .container_runtime import ContainerRuntime, RuntimeAttach, sanitize_diagnostic
from .models import (
    MAX_BACKEND_OUTPUT_CHUNK_BYTES,
    BackendDescriptor,
    BackendExit,
    BackendOutputChunk,
    CleanupResult,
    OutputStreamName,
)

CPU_EXCEEDED_EXIT_CODE: Final = 253
OUTPUT_QUEUE_ITEMS: Final = 16
CLEANUP_TIMEOUT_SECONDS: Final = 10.0


class ContainerExecutionHandle:
    def __init__(
        self,
        *,
        execution_id: str,
        resource: BackendResourceIdentifier,
        runtime: ContainerRuntime,
        container_id: str,
        attach: RuntimeAttach,
        scratch: Path,
        cpu_seconds: float | None,
    ) -> None:
        self._execution_id = execution_id
        self._resource = resource
        self._runtime = runtime
        self._container_id = container_id
        self._attach = attach
        self._scratch = scratch
        self._cpu_seconds = cpu_seconds

        self._output_claimed = False
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=OUTPUT_QUEUE_ITEMS)
        self._pumps = (
            asyncio.create_task(self._pump("stdout", attach.stdout)),
            asyncio.create_task(self._pump("stderr", attach.stderr)),
        )
        self._open_streams = len(self._pumps)

        self._wait_task: asyncio.Task | None = None
        self._terminate_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._termination_reason: TerminationReason | None = None

    @property
    def execution_id(self) -> str:
        return self._execution_id

    @property
    def resource(self) -> BackendResourceIdentifier:
        return self._resource

    @property
    def container_id(self) -> str:
        return self._container_id

    def output(self) -> AsyncIterator[BackendOutputChunk]:
        if self._output_claimed:
            raise RuntimeError("output already has an owner")
        self._output_claimed = True

        async def iterate() -> AsyncIterator[BackendOutputChunk]:
            while True:
                item = await self._queue.get()
                if item is None:
                    self._open_streams -= 1
                    if self._open_streams <= 0:
                        return
                    continue
                yield item

        return iterate()

    async def wait(self) -> BackendExit:
        if self._wait_task is None:
            self._wait_task = asyncio.create_task(self._wait())
        return await asyncio.shield(self._wait_task)

    async def terminate(
        self,
        reason: TerminationReason,
        grace_seconds: float,
    ) -> None:
        if self._terminate_task is None:
            self._termination_reason = reason
            self._terminate_task = asyncio.create_task(
                self._terminate(grace_seconds),
            )
        await asyncio.shield(self._terminate_task)

    async def cleanup(self) -> CleanupResult:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup())
        return await asyncio.shield(self._cleanup_task)

    async def _pump(
        self,
        stream: OutputStreamName,
        reader: asyncio.StreamReader,
    ) -> None:
        try:
            while True:
                data = await reader.read(MAX_BACKEND_OUTPUT_CHUNK_BYTES)
                if not data:
                    break
                await self._queue.put(
                    BackendOutputChunk(stream=stream, data=data),
                )
        finally:
            await self._queue.put(None)

    async def _wait(self) -> BackendExit:
        with suppress(Exception):
            await self._attach.process.wait()
        await asyncio.gather(*self._pumps, return_exceptions=True)

        inspection = await self._runtime.inspect(self._container_id)
        return self._exit_from(inspection)

    def _exit_from(self, inspection: ContainerInspection | None) -> BackendExit:
        reason = self._termination_reason
        if inspection is None:
            return BackendExit(
                exit_code=None,
                native_reason=reason or "shutdown",
            )
        if inspection.oom_killed:
            return BackendExit(exit_code=None, native_reason="memory_limit")
        if inspection.exit_code == CPU_EXCEEDED_EXIT_CODE and self._cpu_seconds:
            return BackendExit(exit_code=None, native_reason="cpu_limit")
        if reason is not None:
            return BackendExit(exit_code=None, native_reason=reason)
        return BackendExit(exit_code=inspection.exit_code)

    async def _terminate(self, grace_seconds: float) -> None:
        await self._runtime.stop(self._container_id, grace_seconds)
        inspection = await self._runtime.inspect(self._container_id)
        if inspection is not None and inspection.running:
            await self._runtime.kill(self._container_id)

    async def _cleanup(self) -> CleanupResult:
        problems: list[str] = []

        inspection = await self._runtime.inspect(self._container_id)
        if inspection is not None and inspection.running:
            if self._termination_reason is None:
                self._termination_reason = "shutdown"
            with suppress(Exception):
                await self._terminate(0.0)

        for pump in self._pumps:
            pump.cancel()
        await asyncio.gather(*self._pumps, return_exceptions=True)

        with suppress(Exception):
            await asyncio.wait_for(
                self._attach.process.wait(),
                timeout=CLEANUP_TIMEOUT_SECONDS,
            )

        try:
            await self._runtime.remove(self._container_id, force=True)
        except Exception as error:  # noqa: BLE001
            problems.append(f"remove failed: {sanitize_diagnostic(str(error))}")

        remaining = await self._runtime.inspect(self._container_id)
        if remaining is not None:
            problems.append("the exact container is still present after removal")

        _remove_scratch(self._scratch)

        if problems:
            return CleanupResult(
                complete=False,
                diagnostic=NativeDiagnostic(
                    code="container-cleanup-incomplete",
                    message=sanitize_diagnostic("; ".join(problems)),
                    platform="posix",
                ),
            )
        return CleanupResult(complete=True)


class ContainerBackend:
    def __init__(
        self,
        descriptor: BackendDescriptor,
        runtime: ContainerRuntime,
        config: ContainerLaunchConfig,
        *,
        host_id: str,
    ) -> None:
        if not isinstance(descriptor, BackendDescriptor):
            raise TypeError("descriptor must be a BackendDescriptor")
        if descriptor.name != "container":
            raise ValueError("ContainerBackend requires a container descriptor")
        if not isinstance(config, ContainerLaunchConfig):
            raise TypeError("config must be a ContainerLaunchConfig")
        if not isinstance(host_id, str) or not host_id.strip():
            raise ValueError("host_id cannot be empty")

        self._descriptor = descriptor
        self._runtime = runtime
        self._config = config
        self._host_id = host_id.strip()

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
    ) -> ContainerExecutionHandle:
        del request
        if not isinstance(context, BackendStartContext):
            raise TypeError("context must be a BackendStartContext")
        cancellation.raise_if_cancelled()

        environment = prepared.environment
        if not environment.valid:
            names = ", ".join(
                sorted(violation.name for violation in environment.violations)
            )
            raise BackendStartError(
                f"child environment was rejected: {names}",
                execution_id=context.execution_id,
                backend="container",
                operation="start",
            )

        ownership_token = secrets.token_hex(32)
        scratch = Path(tempfile.mkdtemp(prefix="truecoder-exec-"))
        os.chmod(scratch, 0o700)
        env_file = scratch / "environment"
        container_id: str | None = None
        transferred = False

        try:
            plan = build_container_plan(
                prepared,
                context,
                self._descriptor,
                self._config,
                ownership_token=ownership_token,
                env_file=env_file,
            )
            _write_env_file(env_file, prepared, prepared.request.limits.cpu_seconds)

            created_id: str = await self._runtime.create(plan)
            container_id = created_id
            inspection = await self._require_created(plan, created_id, context)

            resource = create_container_resource(
                plan,
                container_id=created_id,
                runtime_version=self._runtime_version(),
                host_id=self._host_id,
            )
            mismatches = verify_container_identity(
                resource,
                inspection,
                host_id=self._host_id,
            )
            if mismatches:
                raise BackendStartError(
                    "the created container does not match its exact identity: "
                    + ", ".join(mismatches),
                    execution_id=context.execution_id,
                    backend="container",
                    operation="start",
                )

            cancellation.raise_if_cancelled()
            await register_resource(resource)
            cancellation.raise_if_cancelled()

            _delete_env_file(env_file)
            attach = await self._runtime.start_attached(created_id)
            handle = ContainerExecutionHandle(
                execution_id=context.execution_id,
                resource=resource,
                runtime=self._runtime,
                container_id=created_id,
                attach=attach,
                scratch=scratch,
                cpu_seconds=prepared.request.limits.cpu_seconds,
            )
            transferred = True
            return handle
        except BaseException:
            await self._abort(container_id)
            raise
        finally:
            _delete_env_file(env_file)
            if not transferred:
                _remove_scratch(scratch)

    async def _require_created(
        self,
        plan: ContainerCreatePlan,
        container_id: str,
        context: BackendStartContext,
    ) -> ContainerInspection:
        inspection = await self._runtime.inspect(container_id)
        if inspection is None:
            raise BackendStartError(
                "the created container disappeared before registration",
                execution_id=context.execution_id,
                backend="container",
                operation="start",
            )
        if inspection.state != "created":
            raise BackendStartError(
                f"the container is not stopped before registration: {inspection.state}",
                execution_id=context.execution_id,
                backend="container",
                operation="start",
            )
        if inspection.image_digest != plan.image.digest:
            raise BackendStartError(
                "the container was created from an unexpected image",
                execution_id=context.execution_id,
                backend="container",
                operation="start",
            )
        return inspection

    def _runtime_version(self) -> str:
        descriptor = self._runtime.descriptor
        return descriptor.server_version or descriptor.client_version or "unknown"

    async def _abort(self, container_id: str | None) -> None:
        if container_id is None:
            return
        with suppress(Exception):
            await self._runtime.remove(container_id, force=True)


def _write_env_file(
    path: Path,
    prepared: PreparedExecution,
    cpu_seconds: float | None,
) -> None:
    content = build_env_file_content(prepared, cpu_seconds=cpu_seconds)
    if len(content.encode("utf-8")) > MAX_INSPECT_BYTES:
        raise ValueError("the prepared environment is too large for a sandbox")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def _delete_env_file(path: Path) -> None:
    with suppress(OSError):
        path.unlink()


def _remove_scratch(scratch: Path) -> None:
    with suppress(OSError):
        for child in scratch.iterdir():
            with suppress(OSError):
                child.unlink()
        scratch.rmdir()
