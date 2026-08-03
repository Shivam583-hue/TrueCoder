from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from truecoder.execution.audit.models import BackendResourceIdentifier
from truecoder.execution.backends.base import BackendStartContext
from truecoder.execution.backends.models import (
    BackendDescriptor,
    DiscoveredProgram,
)
from truecoder.execution.backends.posix import PosixBackend
from truecoder.execution.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from truecoder.execution.environment import construct_environment
from truecoder.execution.errors import (
    BackendStartError,
    EnvironmentConstructionError,
)
from truecoder.execution.models import (
    BackendCapabilities,
    ExecutionContext,
    ExecutionLimits,
    ExecutionRequest,
)
from truecoder.execution.preparation import PreparedExecution

ROOT = Path.cwd().resolve()
HELPERS = ROOT / "tests" / "helpers" / "execution"
HOST_ENVIRONMENT = {
    "PATH": os.defpath,
    "LANG": "C.UTF-8",
    "GITHUB_TOKEN": "never-inherit",
}


def _descriptor() -> BackendDescriptor:
    return BackendDescriptor(
        name="posix",
        available=True,
        capabilities=BackendCapabilities(
            filesystem_isolation="unsupported",
            network_isolation="unsupported",
            memory_limits="best_effort",
            cpu_limits="best_effort",
            process_limits="best_effort",
            timeout_enforcement="enforced",
            cancellation="enforced",
            supported_execution_modes=("exec", "shell"),
            supported_filesystem_modes=("host",),
            supported_shells=("posix",),
        ),
        version="test",
    )


def _backend() -> PosixBackend:
    shell_path = shutil.which("sh")
    assert shell_path is not None
    return PosixBackend(
        _descriptor(),
        shells=(
            DiscoveredProgram(
                name="sh",
                path=Path(shell_path),
                shell_kind="posix",
            ),
        ),
    )


def _request(
    argv: tuple[str, ...],
    *,
    directory: Path = ROOT,
    environment: tuple[tuple[str, str], ...] = (),
) -> ExecutionRequest:
    return ExecutionRequest(
        mode="exec",
        argv=argv,
        script=None,
        working_directory=directory,
        limits=ExecutionLimits(
            timeout_seconds=5,
            max_output_bytes=1024 * 1024,
            max_return_bytes=4096,
            termination_grace_seconds=0.05,
        ),
        network_access=True,
        filesystem_mode="host",
        environment=environment,
    )


def _prepared(request: ExecutionRequest) -> PreparedExecution:
    return PreparedExecution(
        request=request,
        backend=_descriptor(),
        environment=construct_environment(
            platform="posix",
            inherited=HOST_ENVIRONMENT,
            requested=request.environment,
        ),
        resolved_shell=None,
    )


def _context(execution_id: str) -> BackendStartContext:
    return BackendStartContext(
        execution=_execution(execution_id),
        audit_run_id=f"run_{execution_id}",
    )


def _execution(execution_id: str) -> ExecutionContext:
    return ExecutionContext(
        execution_id=execution_id,
        tool_call_id=f"call_{execution_id}",
        session_id="session_posix",
        turn_id="turn_posix",
        workspace_id="workspace_posix",
        project_root=ROOT,
        launched_at_utc=datetime.now(timezone.utc),
    )


@unittest.skipUnless(os.name == "posix", "requires POSIX process semantics")
class PosixBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_streams_both_outputs_and_returns_nonzero_as_data(self):
        registered: list[BackendResourceIdentifier] = []
        request = _request(
            (
                sys.executable,
                str(HELPERS / "emit_output.py"),
                "--stdout",
                "hello",
                "--stderr",
                "warning",
                "--exit-code",
                "7",
            )
        )
        handle = await _backend().start(
            _prepared(request),
            request,
            _context("exec_output"),
            CancellationSource().token,
            _registrar(registered),
        )
        try:
            chunks = tuple([chunk async for chunk in handle.output()])
            result = await handle.wait()

            self.assertEqual(result.exit_code, 7)
            self.assertEqual(
                b"".join(chunk.data for chunk in chunks if chunk.stream == "stdout"),
                b"hello",
            )
            self.assertEqual(
                b"".join(chunk.data for chunk in chunks if chunk.stream == "stderr"),
                b"warning",
            )
            self.assertEqual(registered, [handle.resource])
        finally:
            self.assertTrue((await handle.cleanup()).complete)

    async def test_child_receives_filtered_environment_only(self):
        request = _request(
            (
                sys.executable,
                str(HELPERS / "print_environment.py"),
                "PATH",
                "GITHUB_TOKEN",
                "ADDED",
            ),
            environment=(("ADDED", "yes"),),
        )
        handle = await _backend().start(
            _prepared(request),
            request,
            _context("exec_environment"),
            CancellationSource().token,
            _registrar([]),
        )
        try:
            output = b"".join([
                chunk.data
                async for chunk in handle.output()
                if chunk.stream == "stdout"
            ])
            self.assertEqual((await handle.wait()).exit_code, 0)
            values = json.loads(output)
            self.assertEqual(values["PATH"], os.defpath)
            self.assertIsNone(values["GITHUB_TOKEN"])
            self.assertEqual(values["ADDED"], "yes")
        finally:
            await handle.cleanup()

    async def test_registration_happens_before_marker_target_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"

            async def register(_resource: BackendResourceIdentifier) -> None:
                self.assertFalse(marker.exists())
                await asyncio.sleep(0)
                self.assertFalse(marker.exists())

            request = _request(
                (
                    sys.executable,
                    str(HELPERS / "write_marker.py"),
                    str(marker),
                ),
                directory=Path(directory),
            )
            handle = await _backend().start(
                _prepared(request),
                request,
                _context("exec_gate"),
                CancellationSource().token,
                register,
            )
            try:
                await handle.wait()
                self.assertEqual(marker.read_text(encoding="utf-8"), "started")
            finally:
                await handle.cleanup()

    async def test_registration_failure_never_releases_marker_target(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"

            async def reject(_resource: BackendResourceIdentifier) -> None:
                raise RuntimeError("audit unavailable")

            request = _request(
                (
                    sys.executable,
                    str(HELPERS / "write_marker.py"),
                    str(marker),
                ),
                directory=Path(directory),
            )
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                await _backend().start(
                    _prepared(request),
                    request,
                    _context("exec_rejected_gate"),
                    CancellationSource().token,
                    reject,
                )

            self.assertFalse(marker.exists())

    async def test_cancellation_during_registration_never_releases_target(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            source = CancellationSource()

            async def cancel_during_registration(
                _resource: BackendResourceIdentifier,
            ) -> None:
                source.cancel("cancel while blocked")
                await asyncio.sleep(0)

            request = _request(
                (
                    sys.executable,
                    str(HELPERS / "write_marker.py"),
                    str(marker),
                ),
                directory=Path(directory),
            )
            with self.assertRaisesRegex(
                CancellationRequested,
                "cancel while blocked",
            ):
                await _backend().start(
                    _prepared(request),
                    request,
                    _context("exec_cancelled_gate"),
                    source.token,
                    cancel_during_registration,
                )

            self.assertFalse(marker.exists())

    async def test_termination_escalates_and_preserves_first_reason(self):
        request = _request(
            (
                sys.executable,
                str(HELPERS / "ignore_term.py"),
            )
        )
        handle = await _backend().start(
            _prepared(request),
            request,
            _context("exec_terminate"),
            CancellationSource().token,
            _registrar([]),
        )
        output_task = asyncio.create_task(_collect_output(handle))
        await asyncio.sleep(0.05)

        await asyncio.gather(
            handle.terminate("cancellation", 0.05),
            handle.terminate("timeout", 0.05),
        )
        result = await handle.wait()
        output = await output_task
        pid = int(output.decode().strip())

        self.assertEqual(result.native_reason, "cancellation")
        self.assertTrue(await _wait_until_absent(pid))
        self.assertTrue((await handle.cleanup()).complete)

    async def test_missing_executable_is_failed_start_not_exit_127(self):
        registered: list[BackendResourceIdentifier] = []
        request = _request(("/truecoder/does-not-exist",))
        with self.assertRaises(BackendStartError):
            await _backend().start(
                _prepared(request),
                request,
                _context("exec_missing"),
                CancellationSource().token,
                _registrar(registered),
            )

        self.assertEqual(len(registered), 1)

    async def test_sensitive_requested_environment_fails_before_registration(self):
        registered: list[BackendResourceIdentifier] = []
        request = _request(
            (sys.executable, "-c", "pass"),
            environment=(("OPENAI_API_KEY", "secret"),),
        )
        with self.assertRaises(EnvironmentConstructionError):
            await _backend().start(
                _prepared(request),
                request,
                _context("exec_bad_environment"),
                CancellationSource().token,
                _registrar(registered),
            )

        self.assertEqual(registered, [])


def _registrar(
    resources: list[BackendResourceIdentifier],
):
    async def register(resource: BackendResourceIdentifier) -> None:
        resources.append(resource)

    return register


async def _collect_output(handle) -> bytes:
    return b"".join([
        chunk.data async for chunk in handle.output() if chunk.stream == "stdout"
    ])


async def _wait_until_absent(pid: int) -> bool:
    deadline = asyncio.get_running_loop().time() + 1
    while asyncio.get_running_loop().time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.02)
    return False


if __name__ == "__main__":
    unittest.main()
