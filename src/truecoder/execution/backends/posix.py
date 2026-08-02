from __future__ import annotations

import asyncio
import os
import secrets
import signal
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass

from ..audit.models import BackendResourceIdentifier
from ..cancellation import CancellationRequested, CancellationToken
from ..environment import (
    EnvironmentPolicy,
    construct_environment,
)
from ..errors import (
    BackendOperationError,
    BackendStartError,
    BackendTerminationError,
    EnvironmentConstructionError,
    OutputCollectionError,
)
from ..models import (
    ExecutionContext,
    ExecutionRequest,
    NativeDiagnostic,
    ResolvedShellKind,
    TerminationReason,
)
from .base import BackendResourceRegistrar
from .models import (
    MAX_BACKEND_OUTPUT_CHUNK_BYTES,
    BackendDescriptor,
    BackendExit,
    BackendOutputChunk,
    CgroupV2Info,
    CleanupResult,
    DiscoveredProgram,
    DiscoverySnapshot,
    SelectedBackend,
)
from .posix_cgroup import (
    PosixCgroup,
    cleanup_cgroup,
    create_execution_cgroup,
    kill_cgroup,
)
from .posix_identity import create_posix_resource
from .posix_plan import build_posix_launch_plan, plan_to_payload
from .posix_protocol import (
    PosixFrame,
    read_frame_stream,
    write_frame_async,
)

_STARTUP_TIMEOUT_SECONDS = 5.0
_OUTPUT_QUEUE_ITEMS = 16
_CLEANUP_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class _OutputClosed:
    stream: str


@dataclass(frozen=True, slots=True)
class _OutputFailed:
    stream: str
    error: Exception


_QueueItem = BackendOutputChunk | _OutputClosed | _OutputFailed


class PosixExecutionHandle:
    def __init__(
        self,
        *,
        context: ExecutionContext,
        process: asyncio.subprocess.Process,
        resource: BackendResourceIdentifier,
        project_pgid: int,
        status_reader: asyncio.StreamReader,
        status_transport: asyncio.ReadTransport,
        lifetime_fd: int,
        control_fd: int,
        cgroup: PosixCgroup | None,
    ) -> None:
        self._context = context
        self._process = process
        self._resource = resource
        self._project_pgid = project_pgid
        self._status_reader = status_reader
        self._status_transport = status_transport
        self._lifetime_fd = lifetime_fd
        self._control_fd = control_fd
        self._cgroup = cgroup
        self._output_queue: asyncio.Queue[_QueueItem] = asyncio.Queue(
            maxsize=_OUTPUT_QUEUE_ITEMS
        )
        self._output_claimed = False
        self._state_lock = asyncio.Lock()
        self._wait_task: asyncio.Task[BackendExit] | None = None
        self._termination_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[CleanupResult] | None = None
        self._termination_reason: TerminationReason | None = None
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("POSIX backend output pipes were not created")
        self._pump_tasks = (
            asyncio.create_task(self._pump("stdout", process.stdout)),
            asyncio.create_task(self._pump("stderr", process.stderr)),
        )

    @property
    def execution_id(self) -> str:
        return self._context.execution_id

    @property
    def resource(self) -> BackendResourceIdentifier:
        return self._resource

    def output(self) -> AsyncIterator[BackendOutputChunk]:
        if self._output_claimed:
            raise RuntimeError("output already has an owner")
        self._output_claimed = True
        return self._iterate_output()

    async def wait(self) -> BackendExit:
        async with self._state_lock:
            if self._wait_task is None:
                self._wait_task = asyncio.create_task(self._wait_impl())
            task = self._wait_task
        return await asyncio.shield(task)

    async def terminate(
        self,
        reason: TerminationReason,
        grace_seconds: float,
    ) -> None:
        if isinstance(grace_seconds, bool) or not isinstance(
            grace_seconds,
            (int, float),
        ):
            raise TypeError("grace_seconds must be a number")
        if grace_seconds < 0:
            raise ValueError("grace_seconds must not be negative")
        async with self._state_lock:
            if self._termination_task is None:
                self._termination_reason = reason
                self._termination_task = asyncio.create_task(
                    self._terminate_impl(reason, float(grace_seconds))
                )
            task = self._termination_task
        await asyncio.shield(task)

    async def cleanup(self) -> CleanupResult:
        async with self._state_lock:
            if self._cleanup_task is None:
                self._cleanup_task = asyncio.create_task(self._cleanup_impl())
            task = self._cleanup_task
        return await asyncio.shield(task)

    async def _iterate_output(self) -> AsyncIterator[BackendOutputChunk]:
        closed: set[str] = set()
        while len(closed) < 2:
            item = await self._output_queue.get()
            if isinstance(item, BackendOutputChunk):
                yield item
            elif isinstance(item, _OutputClosed):
                closed.add(item.stream)
            else:
                raise OutputCollectionError(
                    f"{item.stream} output pump failed",
                    execution_id=self.execution_id,
                    backend="posix",
                    operation="collect_output",
                ) from item.error

    async def _pump(
        self,
        stream: str,
        reader: asyncio.StreamReader,
    ) -> None:
        try:
            while chunk := await reader.read(MAX_BACKEND_OUTPUT_CHUNK_BYTES):
                await self._output_queue.put(
                    BackendOutputChunk(
                        stream=stream,  # type: ignore[arg-type]
                        data=chunk,
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._output_queue.put(_OutputFailed(stream, exc))
        finally:
            await self._output_queue.put(_OutputClosed(stream))

    async def _wait_impl(self) -> BackendExit:
        try:
            frame = await read_frame_stream(self._status_reader)
        except (EOFError, OSError, ValueError) as exc:
            await self._process.wait()
            if self._termination_reason is not None:
                return BackendExit(
                    exit_code=None,
                    native_reason=self._termination_reason,
                )
            raise BackendOperationError(
                "POSIX supervisor exited without a terminal frame",
                execution_id=self.execution_id,
                backend="posix",
                operation="wait",
            ) from exc
        if frame.type == "ERROR":
            await self._process.wait()
            raise BackendOperationError(
                _frame_error_message(frame),
                execution_id=self.execution_id,
                backend="posix",
                operation=str(frame.payload["operation"]),
            )
        if frame.type != "EXIT":
            raise BackendOperationError(
                f"unexpected POSIX terminal frame: {frame.type}",
                execution_id=self.execution_id,
                backend="posix",
                operation="wait",
            )
        await self._process.wait()
        exit_code = frame.payload["exit_code"]
        native_reason = frame.payload["native_reason"]
        return BackendExit(
            exit_code=exit_code if isinstance(exit_code, int) else None,
            native_reason=(
                native_reason if isinstance(native_reason, str) else None
            ),
        )

    async def _terminate_impl(
        self,
        reason: TerminationReason,
        grace_seconds: float,
    ) -> None:
        if self._process.returncode is not None:
            return
        try:
            await write_frame_async(
                self._control_fd,
                "TERMINATE",
                {
                    "reason": reason,
                    "grace_seconds": grace_seconds,
                },
            )
        except OSError:
            await self._force_terminate(grace_seconds)
        try:
            await asyncio.wait_for(
                asyncio.shield(self.wait()),
                timeout=grace_seconds + _CLEANUP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            await self._force_terminate(grace_seconds)
            try:
                await asyncio.wait_for(
                    asyncio.shield(self.wait()),
                    timeout=_CLEANUP_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                raise BackendTerminationError(
                    "POSIX process tree did not terminate",
                    execution_id=self.execution_id,
                    backend="posix",
                    operation="terminate",
                ) from exc

    async def _force_terminate(self, grace_seconds: float) -> None:
        if self._cgroup is not None:
            with suppress(OSError):
                kill_cgroup(self._cgroup)
        if self._process.returncode is None:
            with suppress(ProcessLookupError):
                self._process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(
                self._process.wait(),
                timeout=max(0.0, grace_seconds),
            )
            return
        except TimeoutError:
            pass
        with suppress(ProcessLookupError):
            os.killpg(self._project_pgid, signal.SIGKILL)
        if self._process.returncode is None:
            with suppress(ProcessLookupError):
                self._process.kill()
        await self._process.wait()

    async def _cleanup_impl(self) -> CleanupResult:
        problems: list[str] = []
        if self._process.returncode is None:
            try:
                await self.terminate(
                    "shutdown",
                    self._context_cleanup_grace(),
                )
            except Exception as exc:  # noqa: BLE001
                problems.append(f"termination failed: {type(exc).__name__}")
        for fd_name in ("_lifetime_fd", "_control_fd"):
            fd = getattr(self, fd_name)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError as exc:
                    problems.append(f"{fd_name} close failed: {exc.errno}")
                setattr(self, fd_name, -1)
        for task in self._pump_tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*self._pump_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(
                result,
                asyncio.CancelledError,
            ):
                problems.append(f"output pump failed: {type(result).__name__}")
        self._status_transport.close()
        if self._cgroup is not None:
            try:
                await _cleanup_cgroup_with_retry(self._cgroup)
            except OSError as exc:
                problems.append(f"cgroup cleanup failed: {exc.errno}")
        if problems:
            return CleanupResult(
                complete=False,
                diagnostic=NativeDiagnostic(
                    code="cleanup-incomplete",
                    message="; ".join(problems)[:4096],
                    platform="posix",
                ),
            )
        return CleanupResult(complete=True)

    def _context_cleanup_grace(self) -> float:
        return 0.5


class PosixBackend:
    def __init__(
        self,
        descriptor: BackendDescriptor,
        *,
        shells: tuple[DiscoveredProgram, ...] = (),
        cgroup_v2: CgroupV2Info | None = None,
        inherited_environment: Mapping[str, str] | None = None,
        environment_policy: EnvironmentPolicy | None = None,
        startup_timeout_seconds: float = _STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(descriptor, BackendDescriptor):
            raise TypeError("descriptor must be BackendDescriptor")
        if descriptor.name != "posix":
            raise ValueError("PosixBackend requires a POSIX descriptor")
        if not isinstance(shells, tuple) or any(
            not isinstance(shell, DiscoveredProgram) for shell in shells
        ):
            raise TypeError("shells must contain DiscoveredProgram values")
        if cgroup_v2 is not None and not isinstance(cgroup_v2, CgroupV2Info):
            raise TypeError("cgroup_v2 must be CgroupV2Info or None")
        if inherited_environment is not None and not isinstance(
            inherited_environment,
            Mapping,
        ):
            raise TypeError("inherited_environment must be a mapping or None")
        if environment_policy is not None and not isinstance(
            environment_policy,
            EnvironmentPolicy,
        ):
            raise TypeError("environment_policy must be EnvironmentPolicy or None")
        if (
            isinstance(startup_timeout_seconds, bool)
            or not isinstance(startup_timeout_seconds, (int, float))
            or startup_timeout_seconds <= 0
        ):
            raise ValueError("startup_timeout_seconds must be positive")
        self._descriptor = descriptor
        self._shells = shells
        self._cgroup_v2 = cgroup_v2
        self._inherited_environment = (
            dict(inherited_environment)
            if inherited_environment is not None
            else None
        )
        self._environment_policy = environment_policy
        self._startup_timeout = float(startup_timeout_seconds)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: DiscoverySnapshot,
        **options: object,
    ) -> PosixBackend:
        if not isinstance(snapshot, DiscoverySnapshot):
            raise TypeError("snapshot must be DiscoverySnapshot")
        return cls(
            snapshot.backend("posix"),
            shells=snapshot.shells,
            cgroup_v2=snapshot.cgroup_v2,
            **options,
        )

    @property
    def descriptor(self) -> BackendDescriptor:
        return self._descriptor

    async def start(
        self,
        request: ExecutionRequest,
        context: ExecutionContext,
        cancellation: CancellationToken,
        register_resource: BackendResourceRegistrar,
    ) -> PosixExecutionHandle:
        _validate_start_inputs(request, context, cancellation, register_resource)
        cancellation.raise_if_cancelled()
        self._validate_host_and_cwd(request, context)
        environment = construct_environment(
            platform="posix",
            inherited=(
                self._inherited_environment
                if self._inherited_environment is not None
                else os.environ
            ),
            requested=request.environment,
            policy=self._environment_policy,
        )
        if not environment.valid:
            names = ", ".join(
                sorted(violation.name for violation in environment.violations)
            )
            raise EnvironmentConstructionError(
                f"child environment was rejected: {names}",
                execution_id=context.execution_id,
                backend="posix",
                operation="construct_environment",
            )
        selected = self._selected_for(request)
        ownership_token = secrets.token_hex(32)
        cgroup = create_execution_cgroup(
            self._cgroup_v2,
            execution_id=context.execution_id,
            ownership_token=ownership_token,
            limits=request.limits,
        )
        plan = build_posix_launch_plan(
            request,
            selected,
            environment,
            self._shells,
            execution_id=context.execution_id,
            cgroup_path=cgroup.path if cgroup is not None else None,
            cgroup_controllers=(
                cgroup.controllers if cgroup is not None else ()
            ),
        )
        resources = _StartResources(cgroup=cgroup)
        try:
            await resources.spawn(plan)
            ready = await resources.read_start_frame(
                self._startup_timeout,
                expected="READY",
                cancellation=cancellation,
            )
            supervisor_pid = int(ready.payload["supervisor_pid"])
            project_pgid = int(ready.payload["project_pgid"])
            if resources.process is None or supervisor_pid != resources.process.pid:
                raise BackendStartError(
                    "POSIX supervisor reported the wrong process identity",
                    execution_id=context.execution_id,
                    backend="posix",
                    operation="start",
                )
            resource = create_posix_resource(
                context,
                supervisor_pid=supervisor_pid,
                project_pgid=project_pgid,
                ownership_token=ownership_token,
                cgroup_path=cgroup.path if cgroup is not None else None,
            )
            cancellation.raise_if_cancelled()
            try:
                await register_resource(resource)
            except BaseException:
                await resources.abort(project_pgid)
                raise
            cancellation.raise_if_cancelled()
            assert resources.gate_fd >= 0
            await write_frame_async(resources.gate_fd, "START", {})
            os.close(resources.gate_fd)
            resources.gate_fd = -1
            started = await resources.read_start_frame(
                self._startup_timeout,
                expected="STARTED",
                cancellation=cancellation,
            )
            if int(started.payload["project_pid"]) != project_pgid:
                raise BackendStartError(
                    "POSIX supervisor started the wrong project leader",
                    execution_id=context.execution_id,
                    backend="posix",
                    operation="start",
                )
            return resources.transfer(
                context=context,
                resource=resource,
                project_pgid=project_pgid,
            )
        except CancellationRequested:
            await resources.abort()
            raise
        except BackendStartError:
            await resources.abort()
            raise
        except (OSError, ValueError, EOFError, TimeoutError) as exc:
            await resources.abort()
            raise BackendStartError(
                "failed to start the POSIX execution backend",
                execution_id=context.execution_id,
                backend="posix",
                operation="start",
            ) from exc

    def _validate_host_and_cwd(
        self,
        request: ExecutionRequest,
        context: ExecutionContext,
    ) -> None:
        if os.name != "posix" or not self._descriptor.available:
            raise BackendStartError(
                "the POSIX backend is unavailable on this host",
                execution_id=context.execution_id,
                backend="posix",
                operation="start",
            )
        try:
            current = request.working_directory.resolve(strict=True)
        except OSError as exc:
            raise BackendStartError(
                "working directory does not exist",
                execution_id=context.execution_id,
                backend="posix",
                operation="validate_cwd",
            ) from exc
        if not current.is_dir():
            raise BackendStartError(
                "working directory is not a directory",
                execution_id=context.execution_id,
                backend="posix",
                operation="validate_cwd",
            )

    def _selected_for(self, request: ExecutionRequest) -> SelectedBackend:
        resolved: ResolvedShellKind | None = None
        if request.mode == "shell":
            if request.shell_kind == "auto":
                supported = self._descriptor.capabilities.supported_shells
                resolved = (
                    "posix"
                    if "posix" in supported
                    else "powershell"
                    if "powershell" in supported
                    else None
                )
            else:
                resolved = request.shell_kind
        return SelectedBackend(
            descriptor=self._descriptor,
            resolved_shell=resolved,
            selection_reason="POSIX backend instance selected by the execution service.",
        )


@dataclass(slots=True)
class _StartResources:
    cgroup: PosixCgroup | None
    process: asyncio.subprocess.Process | None = None
    status_reader: asyncio.StreamReader | None = None
    status_transport: asyncio.ReadTransport | None = None
    config_fd: int = -1
    gate_fd: int = -1
    lifetime_fd: int = -1
    control_fd: int = -1

    async def spawn(self, plan: object) -> None:
        from .posix_plan import PosixLaunchPlan

        if not isinstance(plan, PosixLaunchPlan):
            raise TypeError("plan must be PosixLaunchPlan")
        config_read, self.config_fd = os.pipe()
        gate_read, self.gate_fd = os.pipe()
        lifetime_read, self.lifetime_fd = os.pipe()
        status_read, status_write = os.pipe()
        control_read, self.control_fd = os.pipe()
        child_fds = (
            config_read,
            gate_read,
            lifetime_read,
            status_write,
            control_read,
        )
        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-m",
                "truecoder.execution.backends.posix_supervisor",
                "--config-fd",
                str(config_read),
                "--gate-fd",
                str(gate_read),
                "--lifetime-fd",
                str(lifetime_read),
                "--status-fd",
                str(status_write),
                "--control-fd",
                str(control_read),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=child_fds,
                close_fds=True,
                start_new_session=True,
                env=_supervisor_environment(),
            )
        finally:
            for fd in child_fds:
                with suppress(OSError):
                    os.close(fd)
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: protocol,
            os.fdopen(status_read, "rb", buffering=0),
        )
        self.status_reader = reader
        self.status_transport = transport
        await write_frame_async(self.config_fd, "CONFIG", plan_to_payload(plan))
        os.close(self.config_fd)
        self.config_fd = -1

    async def read_start_frame(
        self,
        timeout: float,
        *,
        expected: str,
        cancellation: CancellationToken,
    ) -> PosixFrame:
        if self.status_reader is None:
            raise RuntimeError("status reader was not initialized")
        frame_task = asyncio.create_task(read_frame_stream(self.status_reader))
        cancellation_task = asyncio.create_task(cancellation.wait())
        done, _pending = await asyncio.wait(
            {frame_task, cancellation_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            frame_task.cancel()
            cancellation_task.cancel()
            await asyncio.gather(
                frame_task,
                cancellation_task,
                return_exceptions=True,
            )
            raise TimeoutError
        if cancellation_task in done:
            reason = cancellation_task.result()
            frame_task.cancel()
            await asyncio.gather(frame_task, return_exceptions=True)
            raise CancellationRequested(reason)
        cancellation_task.cancel()
        await asyncio.gather(cancellation_task, return_exceptions=True)
        frame = frame_task.result()
        if frame.type == "ERROR":
            raise BackendStartError(
                _frame_error_message(frame),
                backend="posix",
                operation=str(frame.payload["operation"]),
            )
        if frame.type != expected:
            raise BackendStartError(
                f"expected {expected}, received {frame.type}",
                backend="posix",
                operation="start",
            )
        return frame

    def transfer(
        self,
        *,
        context: ExecutionContext,
        resource: BackendResourceIdentifier,
        project_pgid: int,
    ) -> PosixExecutionHandle:
        if (
            self.process is None
            or self.status_reader is None
            or self.status_transport is None
        ):
            raise RuntimeError("start resources are incomplete")
        handle = PosixExecutionHandle(
            context=context,
            process=self.process,
            resource=resource,
            project_pgid=project_pgid,
            status_reader=self.status_reader,
            status_transport=self.status_transport,
            lifetime_fd=self.lifetime_fd,
            control_fd=self.control_fd,
            cgroup=self.cgroup,
        )
        self.process = None
        self.status_reader = None
        self.status_transport = None
        self.lifetime_fd = -1
        self.control_fd = -1
        self.cgroup = None
        return handle

    async def abort(self, project_pgid: int | None = None) -> None:
        for fd_name in ("config_fd", "gate_fd", "lifetime_fd", "control_fd"):
            fd = getattr(self, fd_name)
            if fd >= 0:
                with suppress(OSError):
                    os.close(fd)
                setattr(self, fd_name, -1)
        if self.cgroup is not None:
            with suppress(OSError):
                kill_cgroup(self.cgroup)
        if project_pgid is not None:
            with suppress(ProcessLookupError):
                os.killpg(project_pgid, signal.SIGKILL)
        if self.process is not None and self.process.returncode is None:
            with suppress(ProcessLookupError):
                self.process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 1)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        if self.status_transport is not None:
            self.status_transport.close()
        if self.cgroup is not None:
            with suppress(OSError):
                await _cleanup_cgroup_with_retry(self.cgroup)
        self.cgroup = None


async def _cleanup_cgroup_with_retry(cgroup: PosixCgroup) -> None:
    last_error: OSError | None = None
    for _attempt in range(20):
        try:
            cleanup_cgroup(cgroup)
            return
        except OSError as exc:
            last_error = exc
            await asyncio.sleep(0.025)
    assert last_error is not None
    raise last_error


def _frame_error_message(frame: PosixFrame) -> str:
    return (
        f"{frame.payload.get('operation', 'supervisor')} failed: "
        f"{frame.payload.get('message', 'unknown failure')}"
    )


def _supervisor_environment() -> dict[str, str]:
    environment = {
        "PATH": os.defpath,
        "PYTHONIOENCODING": "utf-8",
    }
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "TZ"):
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value
    return environment


def _validate_start_inputs(
    request: object,
    context: object,
    cancellation: object,
    register_resource: object,
) -> None:
    if not isinstance(request, ExecutionRequest):
        raise TypeError("request must be ExecutionRequest")
    if not isinstance(context, ExecutionContext):
        raise TypeError("context must be ExecutionContext")
    if not isinstance(cancellation, CancellationToken):
        raise TypeError("cancellation must be CancellationToken")
    if not callable(register_resource):
        raise TypeError("register_resource must be callable")
