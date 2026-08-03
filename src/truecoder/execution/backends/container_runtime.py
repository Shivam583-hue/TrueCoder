from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from ..errors import BackendOperationError
from .container_dialects import (
    docker_create_argv,
    docker_inspect_argv,
    docker_kill_argv,
    docker_list_managed_argv,
    docker_remove_argv,
    docker_start_attach_argv,
    docker_stop_argv,
    parse_container_id,
    parse_docker_inspect,
)
from .container_models import (
    LABEL_MANAGED,
    ContainerCreatePlan,
    ContainerInspection,
)
from .models import ContainerRuntimeInfo

MAX_CLIENT_OUTPUT_BYTES: Final = 1024 * 1024
DEFAULT_CLIENT_TIMEOUT: Final = 30.0
MAX_DIAGNOSTIC_CHARS: Final = 2048

CLIENT_ENVIRONMENT: Final[dict[str, str]] = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "HOME": "/nonexistent",
}


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


@dataclass(slots=True)
class RuntimeAttach:
    process: asyncio.subprocess.Process
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader


@runtime_checkable
class ContainerRuntime(Protocol):
    @property
    def descriptor(self) -> ContainerRuntimeInfo: ...

    async def create(self, plan: ContainerCreatePlan) -> str: ...

    async def start_attached(self, container_id: str) -> RuntimeAttach: ...

    async def inspect(self, container_id: str) -> ContainerInspection | None: ...

    async def stop(self, container_id: str, grace_seconds: float) -> None: ...

    async def kill(self, container_id: str) -> None: ...

    async def remove(self, container_id: str, *, force: bool) -> None: ...


class DockerRuntime:
    def __init__(
        self,
        descriptor: ContainerRuntimeInfo,
        *,
        timeout_seconds: float = DEFAULT_CLIENT_TIMEOUT,
    ) -> None:
        if not isinstance(descriptor, ContainerRuntimeInfo):
            raise TypeError("descriptor must be a ContainerRuntimeInfo")
        if descriptor.name != "docker":
            raise ValueError("this dialect only supports docker")
        if not isinstance(descriptor.executable, Path):
            raise TypeError("descriptor.executable must be a pathlib.Path")
        self._descriptor = descriptor
        self._timeout = float(timeout_seconds)

    @property
    def descriptor(self) -> ContainerRuntimeInfo:
        return self._descriptor

    async def create(self, plan: ContainerCreatePlan) -> str:
        result = await self._run(docker_create_argv(plan))
        self._require_success(result, "create")
        return parse_container_id(result.stdout)

    async def start_attached(self, container_id: str) -> RuntimeAttach:
        argv = docker_start_attach_argv(container_id)
        process = await asyncio.create_subprocess_exec(
            str(self._descriptor.executable),
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(CLIENT_ENVIRONMENT),
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            await _terminate_client(process)
            raise BackendOperationError(
                "the runtime client did not expose output pipes",
                backend="container",
                operation="start_attached",
            )
        return RuntimeAttach(
            process=process,
            stdout=process.stdout,
            stderr=process.stderr,
        )

    async def inspect(self, container_id: str) -> ContainerInspection | None:
        result = await self._run(docker_inspect_argv(container_id))
        if not result.succeeded:
            if _is_absent(result):
                return None
            self._require_success(result, "inspect")
        return parse_docker_inspect(result.stdout)

    async def stop(self, container_id: str, grace_seconds: float) -> None:
        result = await self._run(docker_stop_argv(container_id, grace_seconds))
        if not result.succeeded and not _is_absent(result):
            self._require_success(result, "stop")

    async def kill(self, container_id: str) -> None:
        result = await self._run(docker_kill_argv(container_id))
        if not result.succeeded and not _is_absent(result):
            if "is not running" in result.stderr:
                return
            self._require_success(result, "kill")

    async def remove(self, container_id: str, *, force: bool) -> None:
        result = await self._run(docker_remove_argv(container_id, force=force))
        if not result.succeeded and not _is_absent(result):
            self._require_success(result, "remove")

    async def list_managed(self) -> tuple[str, ...]:
        result = await self._run(docker_list_managed_argv(LABEL_MANAGED))
        self._require_success(result, "list")
        return tuple(
            line.strip() for line in result.stdout.splitlines() if line.strip()
        )

    async def _run(self, argv: tuple[str, ...]) -> RuntimeResult:
        process = await asyncio.create_subprocess_exec(
            str(self._descriptor.executable),
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(CLIENT_ENVIRONMENT),
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._timeout,
            )
        except (TimeoutError, asyncio.CancelledError):
            await _terminate_client(process)
            raise
        return RuntimeResult(
            exit_code=process.returncode if process.returncode is not None else 1,
            stdout=_bounded(stdout),
            stderr=_bounded(stderr),
        )

    def _require_success(self, result: RuntimeResult, operation: str) -> None:
        if result.succeeded:
            return
        raise BackendOperationError(
            f"the container runtime failed to {operation}",
            backend="container",
            operation=operation,
            diagnostic=None,
        )


def sanitize_diagnostic(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_DIAGNOSTIC_CHARS:
        return collapsed
    return collapsed[:MAX_DIAGNOSTIC_CHARS] + "...[truncated]"


def _bounded(payload: bytes) -> str:
    if len(payload) > MAX_CLIENT_OUTPUT_BYTES:
        payload = payload[:MAX_CLIENT_OUTPUT_BYTES]
    return payload.decode("utf-8", errors="replace")


def _is_absent(result: RuntimeResult) -> bool:
    stderr = result.stderr.lower()
    return "no such container" in stderr or "not found" in stderr


async def _terminate_client(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with suppress(OSError, ProcessLookupError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    with suppress(ProcessLookupError):
        process.kill()
    with suppress(Exception):
        await process.wait()
