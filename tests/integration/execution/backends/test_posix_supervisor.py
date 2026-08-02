from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from truecoder.execution.backends.posix_plan import (
    POSIX_PROTOCOL_VERSION,
    PosixLaunchPlan,
    plan_to_payload,
)
from truecoder.execution.backends.posix_protocol import (
    read_frame_stream,
    write_frame_fd,
)
from truecoder.execution.models import ExecutionLimits

ROOT = Path.cwd().resolve()
HELPERS = ROOT / "tests" / "helpers" / "execution"


@unittest.skipUnless(os.name == "posix", "requires POSIX process semantics")
class PosixSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_waits_behind_start_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            process, reader, transport, fds = await self._spawn(
                _plan(
                    (
                        sys.executable,
                        str(HELPERS / "write_marker.py"),
                        str(marker),
                    ),
                    working_directory=Path(directory),
                )
            )
            try:
                ready = await asyncio.wait_for(read_frame_stream(reader), 2)
                self.assertEqual(ready.type, "READY")
                self.assertFalse(marker.exists())

                write_frame_fd(fds["gate"], "START", {})
                started = await asyncio.wait_for(read_frame_stream(reader), 2)
                exited = await asyncio.wait_for(read_frame_stream(reader), 2)

                self.assertEqual(started.type, "STARTED")
                self.assertEqual(exited.payload["exit_code"], 0)
                self.assertEqual(marker.read_text(encoding="utf-8"), "started")
                self.assertEqual(await asyncio.wait_for(process.wait(), 2), 0)
            finally:
                await _close_harness(process, transport, fds)

    async def test_closed_gate_never_executes_project_code(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            process, reader, transport, fds = await self._spawn(
                _plan(
                    (
                        sys.executable,
                        str(HELPERS / "write_marker.py"),
                        str(marker),
                    ),
                    working_directory=Path(directory),
                )
            )
            try:
                self.assertEqual(
                    (await asyncio.wait_for(read_frame_stream(reader), 2)).type,
                    "READY",
                )
                os.close(fds.pop("gate"))

                self.assertEqual(await asyncio.wait_for(process.wait(), 2), 0)
                self.assertFalse(marker.exists())
            finally:
                await _close_harness(process, transport, fds)

    async def test_missing_executable_is_a_start_error_frame(self):
        process, reader, transport, fds = await self._spawn(
            _plan(("/truecoder/does-not-exist",))
        )
        try:
            self.assertEqual(
                (await asyncio.wait_for(read_frame_stream(reader), 2)).type,
                "READY",
            )
            write_frame_fd(fds["gate"], "START", {})
            error = await asyncio.wait_for(read_frame_stream(reader), 2)

            self.assertEqual(error.type, "ERROR")
            self.assertEqual(error.payload["operation"], "exec")
            self.assertFalse(error.payload["command_started"])
            self.assertEqual(await asyncio.wait_for(process.wait(), 2), 1)
        finally:
            await _close_harness(process, transport, fds)

    async def test_lifetime_eof_kills_the_complete_project_group(self):
        process, reader, transport, fds = await self._spawn(
            _plan(
                (
                    sys.executable,
                    str(HELPERS / "spawn_tree.py"),
                ),
                grace_seconds=0.05,
            )
        )
        try:
            self.assertEqual(
                (await asyncio.wait_for(read_frame_stream(reader), 2)).type,
                "READY",
            )
            write_frame_fd(fds["gate"], "START", {})
            self.assertEqual(
                (await asyncio.wait_for(read_frame_stream(reader), 2)).type,
                "STARTED",
            )
            assert process.stdout is not None
            pids = json.loads(
                (await asyncio.wait_for(process.stdout.readline(), 2)).decode()
            )
            os.close(fds.pop("lifetime"))

            exit_frame = await asyncio.wait_for(read_frame_stream(reader), 3)
            self.assertEqual(exit_frame.payload["native_reason"], "shutdown")
            self.assertEqual(await asyncio.wait_for(process.wait(), 3), 0)
            for pid in pids.values():
                self.assertTrue(await _wait_until_absent(pid))
        finally:
            await _close_harness(process, transport, fds)

    async def _spawn(
        self,
        plan: PosixLaunchPlan,
    ) -> tuple[
        asyncio.subprocess.Process,
        asyncio.StreamReader,
        asyncio.ReadTransport,
        dict[str, int],
    ]:
        config_read, config_write = os.pipe()
        gate_read, gate_write = os.pipe()
        lifetime_read, lifetime_write = os.pipe()
        status_read, status_write = os.pipe()
        control_read, control_write = os.pipe()
        process = await asyncio.create_subprocess_exec(
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
            pass_fds=(
                config_read,
                gate_read,
                lifetime_read,
                status_write,
                control_read,
            ),
            close_fds=True,
            start_new_session=True,
            env={"PATH": os.defpath},
        )
        for fd in (
            config_read,
            gate_read,
            lifetime_read,
            status_write,
            control_read,
        ):
            os.close(fd)
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: protocol,
            os.fdopen(status_read, "rb", buffering=0),
        )
        write_frame_fd(config_write, "CONFIG", plan_to_payload(plan))
        os.close(config_write)
        return (
            process,
            reader,
            transport,
            {
                "gate": gate_write,
                "lifetime": lifetime_write,
                "control": control_write,
            },
        )


def _plan(
    argv: tuple[str, ...],
    *,
    working_directory: Path = ROOT,
    grace_seconds: float = 0.2,
) -> PosixLaunchPlan:
    return PosixLaunchPlan(
        protocol_version=POSIX_PROTOCOL_VERSION,
        execution_id="exec_supervisor",
        argv=argv,
        working_directory=working_directory,
        environment=(
            ("PATH", os.defpath),
            ("PYTHONIOENCODING", "utf-8"),
        ),
        limits=ExecutionLimits(
            timeout_seconds=5,
            max_output_bytes=1024 * 1024,
            max_return_bytes=1024,
            termination_grace_seconds=grace_seconds,
        ),
        shell_kind=None,
    )


async def _close_harness(
    process: asyncio.subprocess.Process,
    transport: asyncio.ReadTransport,
    fds: dict[str, int],
) -> None:
    for fd in fds.values():
        try:
            os.close(fd)
        except OSError:
            pass
    if process.returncode is None:
        process.kill()
        await process.wait()
    transport.close()


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
