from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from tests.contract.execution.backend_contract import (
    BackendContractCase,
    BackendContractMixin,
    BackendContractTestCase,
    BackendContractTracker,
    TrackingBackend,
)
from truecoder.execution.backends.base import BackendStartContext
from truecoder.execution.backends.container import ContainerBackend
from truecoder.execution.backends.container_models import ContainerBackendFacts
from truecoder.execution.backends.container_plan import (
    ContainerLaunchConfig,
    load_image_lock,
)
from truecoder.execution.backends.container_runtime import DockerRuntime
from truecoder.execution.backends.models import (
    BackendDescriptor,
    BackendExit,
    BackendOutputChunk,
    ContainerRuntimeInfo,
)
from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.environment import construct_environment
from truecoder.execution.models import (
    ExecutionContext,
    ExecutionLimits,
    ExecutionRequest,
)
from truecoder.execution.preparation import PreparedExecution

REPOSITORY = Path(__file__).resolve().parents[3]
IMAGE_LOCK = REPOSITORY / "container" / "image.lock"


def _availability() -> tuple[bool, str]:
    executable = shutil.which("docker")
    if executable is None:
        return False, "docker is unavailable"
    if not IMAGE_LOCK.exists():
        return False, "the execution image lock is unavailable"
    try:
        image = load_image_lock(IMAGE_LOCK)
        probe = subprocess.run(
            [executable, "image", "inspect", image.digest],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return False, str(error)
    if probe.returncode != 0:
        return False, "the pinned execution image is unavailable"
    return True, ""


AVAILABLE, SKIP_REASON = _availability()


@unittest.skipUnless(AVAILABLE, SKIP_REASON)
class ContainerBackendContractTests(
    BackendContractMixin,
    BackendContractTestCase,
):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="tc-contract-container-"))
        os.chmod(self.workspace, 0o755)
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)

        image = load_image_lock(IMAGE_LOCK)
        executable = shutil.which("docker")
        assert executable is not None
        runtime_info = ContainerRuntimeInfo(
            name="docker",
            executable=Path(executable),
            client_version="verified",
            server_version="verified",
            daemon_reachable=True,
            rootless="unknown",
        )
        facts = ContainerBackendFacts(
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
        self.descriptor = BackendDescriptor(
            name="container",
            available=True,
            capabilities=facts.capabilities(),
            version="verified",
            runtime=runtime_info,
        )
        self.backend = ContainerBackend(
            self.descriptor,
            DockerRuntime(runtime_info),
            ContainerLaunchConfig(image=image),
            host_id="contract-host",
        )

    async def make_backend_case(
        self,
        *,
        exit_code: int = 0,
    ) -> BackendContractCase:
        tracker = BackendContractTracker()
        request = self._request(
            f"printf 'hello\\n'; exit {exit_code}",
        )
        return BackendContractCase(
            backend=TrackingBackend(self.backend, tracker),
            prepared=self._prepared(request),
            request=request,
            context=self._context("success"),
            cancellation=CancellationSource().token,
            tracker=tracker,
            expected_output=(
                BackendOutputChunk(stream="stdout", data=b"hello\n"),
            ),
            expected_exit=BackendExit(exit_code=exit_code),
            register_resource=self._registrar(tracker),
        )

    async def make_failing_start_case(
        self,
        *,
        cancelled: bool,
    ) -> BackendContractCase:
        tracker = BackendContractTracker()
        source = CancellationSource()
        if cancelled:
            source.cancel("contract cancellation")
        request = self._request(
            "printf unreachable",
            filesystem_mode="workspace-write",
        )
        return BackendContractCase(
            backend=TrackingBackend(self.backend, tracker),
            prepared=self._prepared(request),
            request=request,
            context=self._context("failure"),
            cancellation=source.token,
            tracker=tracker,
            expected_output=(),
            expected_exit=BackendExit(exit_code=0),
            register_resource=self._registrar(tracker),
        )

    def _request(
        self,
        script: str,
        *,
        filesystem_mode: str = "workspace-read",
    ) -> ExecutionRequest:
        return ExecutionRequest(
            mode="shell",
            argv=None,
            script=script,
            working_directory=self.workspace,
            limits=ExecutionLimits(
                timeout_seconds=30,
                max_output_bytes=4096,
                max_return_bytes=2048,
                memory_bytes=128 * 1024 * 1024,
                max_processes=16,
                termination_grace_seconds=0.1,
            ),
            network_access=False,
            filesystem_mode=filesystem_mode,
        )

    def _prepared(self, request: ExecutionRequest) -> PreparedExecution:
        return PreparedExecution(
            request=request,
            backend=self.descriptor,
            environment=construct_environment(
                platform="posix",
                inherited={},
                requested=(),
            ),
            resolved_shell="posix",
        )

    def _context(self, suffix: str) -> BackendStartContext:
        name = self._testMethodName.replace("_", "-")
        return BackendStartContext(
            execution=ExecutionContext(
                execution_id=f"exec-contract-{name}-{suffix}",
                tool_call_id=f"call-contract-{name}-{suffix}",
                session_id="session-contract",
                turn_id="turn-contract",
                workspace_id="workspace-contract",
                project_root=self.workspace,
                launched_at_utc=datetime.now(UTC),
            ),
            audit_run_id=f"run-contract-{name}-{suffix}",
        )

    @staticmethod
    def _registrar(tracker: BackendContractTracker):
        async def register(resource) -> None:
            tracker.resource_registrations += 1
            tracker.registered_resource = resource
            tracker.lifecycle_events.append("registered")

        return register


if __name__ == "__main__":
    unittest.main()
