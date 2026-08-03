from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from truecoder.execution.backends.base import BackendStartContext
from truecoder.execution.backends.container import ContainerBackend
from truecoder.execution.backends.container_models import (
    LABEL_MANAGED,
    ContainerBackendFacts,
)
from truecoder.execution.backends.container_plan import (
    ContainerLaunchConfig,
    load_image_lock,
)
from truecoder.execution.backends.container_runtime import DockerRuntime
from truecoder.execution.backends.models import BackendDescriptor, ContainerRuntimeInfo
from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.environment import construct_environment
from truecoder.execution.errors import BackendStartError
from truecoder.execution.models import (
    ExecutionContext,
    ExecutionLimits,
    ExecutionRequest,
)
from truecoder.execution.preparation import PreparedExecution

REPOSITORY = Path(__file__).resolve().parents[3]
IMAGE_LOCK = REPOSITORY / "container" / "image.lock"


def _docker() -> Path | None:
    found = shutil.which("docker")
    return Path(found) if found else None


def _runtime_available() -> tuple[bool, str]:
    executable = _docker()
    if executable is None:
        return False, "docker client is not installed"
    if not IMAGE_LOCK.exists():
        return False, "container/image.lock is missing"
    try:
        image = load_image_lock(IMAGE_LOCK)
    except (OSError, ValueError) as error:
        return False, f"the image lock is unreadable: {error}"

    probe = subprocess.run(
        [str(executable), "image", "inspect", image.digest],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if probe.returncode != 0:
        return False, "the pinned execution image is not present locally"
    return True, ""


AVAILABLE, SKIP_REASON = _runtime_available()


def facts(image) -> ContainerBackendFacts:
    return ContainerBackendFacts(
        runtime="docker",
        runtime_version="verified",
        image=image,
        supports_read_only_root=True,
        supports_bind_mounts=True,
        supports_tmpfs=True,
        supports_capability_drop=True,
        supports_no_new_privileges=True,
        supports_none_network=True,
        supports_memory_limit=True,
        supports_pids_limit=True,
        cpu_enforcement="best_effort",
        dialect_implemented=True,
        daemon_reachable=True,
        platform_supported=True,
    )


@unittest.skipUnless(AVAILABLE, SKIP_REASON)
class ContainerSandboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.image = load_image_lock(IMAGE_LOCK)
        self.executable = _docker()
        assert self.executable is not None

        self.runtime_info = ContainerRuntimeInfo(
            name="docker",
            executable=self.executable,
            client_version="verified",
            server_version="verified",
            daemon_reachable=True,
            rootless="unknown",
        )
        self.descriptor = BackendDescriptor(
            name="container",
            available=True,
            capabilities=facts(self.image).capabilities(),
            version="verified",
            runtime=self.runtime_info,
        )
        self.runtime = DockerRuntime(self.runtime_info)
        self.backend = ContainerBackend(
            self.descriptor,
            self.runtime,
            ContainerLaunchConfig(image=self.image),
            host_id="sandbox-host",
        )

        self.workspace = Path(tempfile.mkdtemp(prefix="tc-sandbox-ws-"))
        os.chmod(self.workspace, 0o755)
        (self.workspace / "inside.txt").write_text("workspace-content\n")

        self.outside = Path(tempfile.mkdtemp(prefix="tc-sandbox-host-"))
        os.chmod(self.outside, 0o755)
        self.secret = self.outside / "canary.txt"
        self.secret.write_text("TOP-SECRET-CANARY\n")

        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)
        self.addCleanup(self.assert_no_leaked_containers)

    def assert_no_leaked_containers(self) -> None:
        probe = subprocess.run(
            [
                str(self.executable),
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label={LABEL_MANAGED}=true",
            ],
            capture_output=True,
            timeout=60,
            check=False,
            text=True,
        )
        self.assertEqual(probe.stdout.strip(), "")

    def request(
        self,
        script: str,
        *,
        filesystem_mode: str = "workspace-read",
        network_access: bool = False,
        memory_bytes: int | None = 256 * 1024 * 1024,
        max_processes: int | None = 32,
        timeout_seconds: float = 90.0,
    ) -> ExecutionRequest:
        return ExecutionRequest(
            mode="shell",
            argv=None,
            script=script,
            working_directory=self.workspace,
            limits=ExecutionLimits(
                timeout_seconds=timeout_seconds,
                max_output_bytes=1 << 20,
                max_return_bytes=64 * 1024,
                memory_bytes=memory_bytes,
                max_processes=max_processes,
                termination_grace_seconds=1,
            ),
            network_access=network_access,
            filesystem_mode=filesystem_mode,
        )

    async def run_script(self, script: str, label: str = "sandbox", **options):
        execution_request = self.request(script, **options)
        prepared = PreparedExecution(
            request=execution_request,
            backend=self.descriptor,
            environment=construct_environment(
                platform="posix",
                inherited={},
                requested=execution_request.environment,
            ),
            resolved_shell="posix",
        )
        context = BackendStartContext(
            execution=ExecutionContext(
                execution_id=f"exec-{label}",
                tool_call_id=f"call-{label}",
                session_id="session-sandbox",
                turn_id="turn-sandbox",
                workspace_id="workspace-sandbox",
                project_root=self.workspace,
                launched_at_utc=datetime.now(UTC),
            ),
            audit_run_id=f"run_{label}",
        )
        registered = []

        async def register(resource) -> None:
            registered.append(resource)

        handle = await self.backend.start(
            prepared,
            execution_request,
            context,
            CancellationSource().token,
            register,
        )
        try:
            chunks = [chunk async for chunk in handle.output()]
            stdout = b"".join(
                chunk.data for chunk in chunks if chunk.stream == "stdout"
            ).decode("utf-8", errors="replace")
            stderr = b"".join(
                chunk.data for chunk in chunks if chunk.stream == "stderr"
            ).decode("utf-8", errors="replace")
            exit_status = await handle.wait()
        finally:
            cleanup = await handle.cleanup()

        self.assertTrue(cleanup.complete)
        self.assertEqual(len(registered), 1)
        return stdout, stderr, exit_status, registered[0]

    async def test_a_successful_command_runs_non_root_in_the_workspace(self):
        stdout, _stderr, exit_status, resource = await self.run_script(
            "id -u; pwd; cat inside.txt",
            label="basic",
        )

        self.assertEqual(exit_status.exit_code, 0)
        self.assertIn("65532", stdout)
        self.assertIn("/workspace", stdout)
        self.assertIn("workspace-content", stdout)
        self.assertEqual(resource.backend, "container")
        self.assertEqual(dict(resource.native_details)["runtime"], "docker")

    async def test_a_host_secret_outside_the_workspace_is_unreadable(self):
        stdout, stderr, _exit, _resource = await self.run_script(
            f"cat {self.secret}",
            label="canary",
        )

        self.assertNotIn("TOP-SECRET-CANARY", stdout)
        self.assertNotIn("TOP-SECRET-CANARY", stderr)

    async def test_workspace_read_cannot_mutate_the_host_tree(self):
        _stdout, _stderr, exit_status, _resource = await self.run_script(
            "echo mutated > /workspace/inside.txt",
            label="readonly",
        )

        self.assertNotEqual(exit_status.exit_code, 0)
        self.assertEqual(
            (self.workspace / "inside.txt").read_text(),
            "workspace-content\n",
        )

    async def test_workspace_write_can_only_write_inside_the_workspace(self):
        os.chmod(self.workspace, 0o777)

        stdout, _stderr, exit_status, _resource = await self.run_script(
            "echo created > /workspace/new.txt && echo INSIDE-OK; "
            "echo outside > /outside.txt 2>/dev/null || echo OUTSIDE-DENIED",
            label="writable",
            filesystem_mode="workspace-write",
        )

        self.assertEqual(exit_status.exit_code, 0)
        self.assertIn("INSIDE-OK", stdout)
        self.assertIn("OUTSIDE-DENIED", stdout)
        self.assertEqual((self.workspace / "new.txt").read_text(), "created\n")

    async def test_workspace_write_is_refused_when_the_host_denies_it(self):
        os.chmod(self.workspace, 0o755)

        with self.assertRaises(BackendStartError) as caught:
            await self.run_script(
                "echo x > /workspace/denied.txt",
                label="write-refused",
                filesystem_mode="workspace-write",
            )

        self.assertIn("workspace-write requires", str(caught.exception))

    async def test_the_root_filesystem_is_read_only(self):
        stdout, _stderr, _exit, _resource = await self.run_script(
            "for target in /etc/probe /usr/probe /root/probe; do "
            "echo x > $target 2>/dev/null && echo WROTE-$target || echo DENIED-$target; "
            "done",
            label="rootfs",
        )

        self.assertNotIn("WROTE-", stdout)
        self.assertEqual(stdout.count("DENIED-"), 3)

    async def test_only_approved_tmpfs_locations_are_writable(self):
        stdout, _stderr, exit_status, _resource = await self.run_script(
            "echo x > /tmp/ok && echo TMP-OK; echo x > /run/ok && echo RUN-OK",
            label="tmpfs",
        )

        self.assertEqual(exit_status.exit_code, 0)
        self.assertIn("TMP-OK", stdout)
        self.assertIn("RUN-OK", stdout)

    async def test_network_denial_blocks_a_real_external_canary(self):
        stdout, _stderr, _exit, _resource = await self.run_script(
            "wget -q -T 5 -O- http://example.com >/dev/null 2>&1 "
            "&& echo NETWORK-REACHED || echo NETWORK-DENIED",
            label="network",
        )

        self.assertIn("NETWORK-DENIED", stdout)
        self.assertNotIn("NETWORK-REACHED", stdout)

    async def test_no_runtime_socket_is_visible(self):
        stdout, _stderr, _exit, _resource = await self.run_script(
            "for socket in /var/run/docker.sock /run/docker.sock "
            "/run/containerd/containerd.sock; do "
            "test -e $socket && echo FOUND-$socket; done; echo SCAN-COMPLETE",
            label="sockets",
        )

        self.assertNotIn("FOUND-", stdout)
        self.assertIn("SCAN-COMPLETE", stdout)

    async def test_every_capability_is_dropped(self):
        stdout, _stderr, _exit, _resource = await self.run_script(
            "grep CapEff /proc/self/status",
            label="caps",
        )

        self.assertIn("0000000000000000", stdout)

    async def test_no_new_privileges_is_active(self):
        stdout, _stderr, _exit, _resource = await self.run_script(
            "grep NoNewPrivs /proc/self/status",
            label="nnp",
        )

        self.assertIn("NoNewPrivs:\t1", stdout)

    async def test_exceeding_memory_is_normalized_to_a_memory_limit(self):
        _stdout, _stderr, exit_status, _resource = await self.run_script(
            "python3 -c \"a = bytearray(400 * 1024 * 1024)\"",
            label="memory",
            memory_bytes=64 * 1024 * 1024,
        )

        self.assertEqual(exit_status.native_reason, "memory_limit")
        self.assertIsNone(exit_status.exit_code)

    async def test_a_nonzero_exit_is_ordinary_backend_data(self):
        _stdout, _stderr, exit_status, _resource = await self.run_script(
            "exit 7",
            label="nonzero",
        )

        self.assertEqual(exit_status.exit_code, 7)
        self.assertIsNone(exit_status.native_reason)

    async def test_stdout_and_stderr_stay_separate_and_raw(self):
        stdout, stderr, _exit, _resource = await self.run_script(
            "printf 'to-out'; printf 'to-err' >&2",
            label="streams",
        )

        self.assertEqual(stdout, "to-out")
        self.assertEqual(stderr, "to-err")

    async def test_unicode_output_survives_the_boundary(self):
        stdout, _stderr, _exit, _resource = await self.run_script(
            "printf 'h\\303\\251llo w\\303\\266rld'",
            label="unicode",
        )

        self.assertEqual(stdout, "héllo wörld")

    async def test_the_environment_file_never_survives_the_run(self):
        _stdout, _stderr, _exit, _resource = await self.run_script(
            "env | grep -c PATH",
            label="envfile",
        )

        leftovers = list(Path(tempfile.gettempdir()).glob("truecoder-exec-*"))
        self.assertEqual(leftovers, [])

    async def test_terminating_a_signal_ignoring_command_still_removes_it(self):
        execution_request = self.request(
            "trap '' TERM; while true; do sleep 0.2; done",
            timeout_seconds=90.0,
        )
        prepared = PreparedExecution(
            request=execution_request,
            backend=self.descriptor,
            environment=construct_environment(
                platform="posix",
                inherited={},
                requested=(),
            ),
            resolved_shell="posix",
        )
        context = BackendStartContext(
            execution=ExecutionContext(
                execution_id="exec-terminate",
                tool_call_id="call-terminate",
                session_id="session-sandbox",
                turn_id="turn-sandbox",
                workspace_id="workspace-sandbox",
                project_root=self.workspace,
                launched_at_utc=datetime.now(UTC),
            ),
            audit_run_id="run_terminate",
        )

        async def register(resource) -> None:
            del resource

        handle = await self.backend.start(
            prepared,
            execution_request,
            context,
            CancellationSource().token,
            register,
        )
        container_id = handle.container_id
        drain = asyncio.create_task(_drain(handle))

        await handle.terminate("cancellation", 1.0)
        exit_status = await handle.wait()
        cleanup = await handle.cleanup()
        await drain

        self.assertEqual(exit_status.native_reason, "cancellation")
        self.assertTrue(cleanup.complete)
        self.assertFalse(_container_exists(self.executable, container_id))

    async def test_registration_failure_removes_the_stopped_container(self):
        execution_request = self.request("echo should-never-run > /tmp/marker")
        prepared = PreparedExecution(
            request=execution_request,
            backend=self.descriptor,
            environment=construct_environment(
                platform="posix",
                inherited={},
                requested=(),
            ),
            resolved_shell="posix",
        )
        context = BackendStartContext(
            execution=ExecutionContext(
                execution_id="exec-registrar",
                tool_call_id="call-registrar",
                session_id="session-sandbox",
                turn_id="turn-sandbox",
                workspace_id="workspace-sandbox",
                project_root=self.workspace,
                launched_at_utc=datetime.now(UTC),
            ),
            audit_run_id="run_registrar",
        )
        seen: list[str] = []

        async def refuse(resource) -> None:
            seen.append(dict(resource.native_details)["container_id"])
            raise RuntimeError("durable attachment refused")

        with self.assertRaises(RuntimeError):
            await self.backend.start(
                prepared,
                execution_request,
                context,
                CancellationSource().token,
                refuse,
            )

        self.assertEqual(len(seen), 1)
        self.assertFalse(_container_exists(self.executable, seen[0]))

    async def test_the_container_is_created_stopped_before_registration(self):
        execution_request = self.request("echo ran")
        prepared = PreparedExecution(
            request=execution_request,
            backend=self.descriptor,
            environment=construct_environment(
                platform="posix",
                inherited={},
                requested=(),
            ),
            resolved_shell="posix",
        )
        context = BackendStartContext(
            execution=ExecutionContext(
                execution_id="exec-gate",
                tool_call_id="call-gate",
                session_id="session-sandbox",
                turn_id="turn-sandbox",
                workspace_id="workspace-sandbox",
                project_root=self.workspace,
                launched_at_utc=datetime.now(UTC),
            ),
            audit_run_id="run_gate",
        )
        states: list[str] = []

        async def observe(resource) -> None:
            container_id = dict(resource.native_details)["container_id"]
            states.append(_container_state(self.executable, container_id))

        handle = await self.backend.start(
            prepared,
            execution_request,
            context,
            CancellationSource().token,
            observe,
        )
        try:
            await _drain(handle)
            await handle.wait()
        finally:
            await handle.cleanup()

        self.assertEqual(states, ["created"])


async def _drain(handle) -> None:
    async for _chunk in handle.output():
        pass


def _container_exists(executable: Path, container_id: str) -> bool:
    probe = subprocess.run(
        [str(executable), "inspect", "--type", "container", container_id],
        capture_output=True,
        timeout=60,
        check=False,
    )
    return probe.returncode == 0


def _container_state(executable: Path, container_id: str) -> str:
    probe = subprocess.run(
        [str(executable), "inspect", "--type", "container", container_id],
        capture_output=True,
        timeout=60,
        check=False,
        text=True,
    )
    if probe.returncode != 0:
        return "absent"
    return json.loads(probe.stdout)[0]["State"]["Status"]


if __name__ == "__main__":
    unittest.main()
