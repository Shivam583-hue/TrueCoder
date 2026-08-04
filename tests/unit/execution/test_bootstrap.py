from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from tests.fakes.execution import CollectingEventSink, PreviewCollector
from truecoder.execution.approval import ApprovalResponse, ApprovalService
from truecoder.execution.audit import (
    AuditService,
    BackendResourceIdentifier,
    SQLiteAuditStore,
    TerminalOutcome,
)
from truecoder.execution.backends.container_models import ContainerBackendFacts
from truecoder.execution.backends.container_plan import load_image_lock
from truecoder.execution.backends.models import (
    BackendDescriptor,
    ContainerRuntimeInfo,
    DiscoverySnapshot,
    HostPlatformInfo,
    UnavailableReason,
)
from truecoder.execution.bootstrap import (
    ExecutionBootstrapConfig,
    bootstrap_execution,
)
from truecoder.execution.events import NullEventSink
from truecoder.execution.models import (
    BackendCapabilities,
    ExecutionContext,
    ExecutionLimits,
    ExecutionRequest,
)

DIGEST = "sha256:" + "a" * 64


async def approve(_request):
    return ApprovalResponse.approve()


def capabilities(
    *,
    filesystem_modes=("host",),
    modes=("exec", "shell"),
) -> BackendCapabilities:
    return BackendCapabilities(
        filesystem_isolation="unsupported",
        network_isolation="unsupported",
        memory_limits="best_effort",
        cpu_limits="best_effort",
        process_limits="best_effort",
        timeout_enforcement="enforced",
        cancellation="enforced",
        supported_execution_modes=modes,
        supported_filesystem_modes=filesystem_modes,
        supported_shells=("posix",) if "shell" in modes else (),
    )


def unavailable(name: str) -> BackendDescriptor:
    return BackendDescriptor(
        name=name,  # type: ignore[arg-type]
        available=False,
        capabilities=capabilities(modes=("exec",)),
        unavailable_reasons=(
            UnavailableReason(
                code="unavailable",
                message="backend unavailable",
            ),
        ),
    )


def posix_descriptor(*, available: bool = True) -> BackendDescriptor:
    if not available:
        return unavailable("posix")
    return BackendDescriptor(
        name="posix",
        available=True,
        capabilities=capabilities(),
        version="test",
    )


def snapshot(
    *,
    posix: BackendDescriptor | None = None,
    windows: BackendDescriptor | None = None,
    container: BackendDescriptor | None = None,
    runtimes: tuple[ContainerRuntimeInfo, ...] = (),
) -> DiscoverySnapshot:
    return DiscoverySnapshot(
        host=HostPlatformInfo(
            system="linux",
            family="posix",
            architecture="amd64",
        ),
        shells=(),
        cgroup_v2=None,
        runtimes=runtimes,
        backends=(
            posix or unavailable("posix"),
            windows or unavailable("windows"),
            container or unavailable("container"),
        ),
    )


class BootstrapFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "audit.sqlite3"
        self.image_lock = self.root / "image.lock"
        self.approvals = ApprovalService(approve)

    def config(self, **overrides) -> ExecutionBootstrapConfig:
        values = {
            "audit_database_path": self.database,
            "image_lock_path": self.image_lock,
            **overrides,
        }
        return ExecutionBootstrapConfig(**values)


class ExecutionBootstrapTests(BootstrapFixture):
    async def test_healthy_posix_backend_enables_the_service(self):
        runtime = await bootstrap_execution(
            self.approvals,
            config=self.config(),
            discovery_snapshot=snapshot(posix=posix_descriptor()),
        )

        self.assertTrue(runtime.shell_available)
        self.assertIsNotNone(runtime.service)
        self.assertEqual(
            tuple(backend.descriptor.name for backend in runtime.backends),
            ("posix",),
        )
        self.assertTrue(runtime.health.audit_ready)
        self.assertTrue(runtime.health.recovery_ready)

    async def test_no_available_backend_omits_the_service(self):
        runtime = await bootstrap_execution(
            self.approvals,
            config=self.config(),
            discovery_snapshot=snapshot(),
        )

        self.assertFalse(runtime.shell_available)
        self.assertIsNone(runtime.service)
        self.assertEqual(runtime.health.failure_code, "no_healthy_backend")

    async def test_windows_is_never_registered_on_a_non_windows_host(self):
        windows = BackendDescriptor(
            name="windows",
            available=True,
            capabilities=capabilities(),
            version="test",
        )

        with patch("truecoder.execution.bootstrap.WINDOWS", False):
            runtime = await bootstrap_execution(
                self.approvals,
                config=self.config(),
                discovery_snapshot=snapshot(windows=windows),
            )

        self.assertFalse(runtime.shell_available)
        windows_health = next(
            item for item in runtime.health.backends if item.name == "windows"
        )
        self.assertTrue(windows_health.discovered)
        self.assertFalse(windows_health.registered)
        self.assertEqual(
            windows_health.reasons,
            ("backend requires a windows host",),
        )

    async def test_audit_unavailability_fails_closed(self):
        blocker = self.root / "not-a-directory"
        blocker.write_text("block")

        runtime = await bootstrap_execution(
            self.approvals,
            config=self.config(
                audit_database_path=blocker / "audit.sqlite3"
            ),
            discovery_snapshot=snapshot(posix=posix_descriptor()),
        )

        self.assertFalse(runtime.shell_available)
        self.assertIsNone(runtime.audit)
        self.assertEqual(runtime.health.failure_code, "audit_unavailable")

    async def test_recovery_failure_keeps_every_backend_disabled(self):
        audit = AuditService(SQLiteAuditStore(self.database))
        context = ExecutionContext(
            execution_id="exec-recovery-failure",
            tool_call_id="call-recovery-failure",
            session_id="session-recovery-failure",
            turn_id="turn-recovery-failure",
            workspace_id="workspace-recovery-failure",
            project_root=self.root,
            launched_at_utc=datetime(2026, 8, 3, tzinfo=UTC),
        )
        request = ExecutionRequest(
            mode="exec",
            argv=("true",),
            script=None,
            working_directory=self.root,
            limits=ExecutionLimits(
                timeout_seconds=10,
                max_output_bytes=1024,
                max_return_bytes=512,
            ),
            network_access=False,
            filesystem_mode="host",
        )
        handle = await audit.admit(context, request)
        await audit.attach_resource(
            handle,
            BackendResourceIdentifier(
                version=1,
                backend="posix",
                resource_kind="wrong-kind",
                resource_id=context.execution_id,
                ownership_token="owner-recovery-failure",
                host_id="wrong-host",
                created_at_utc=context.launched_at_utc,
                native_details=(),
            ),
        )

        runtime = await bootstrap_execution(
            self.approvals,
            config=self.config(),
            discovery_snapshot=snapshot(posix=posix_descriptor()),
        )

        self.assertFalse(runtime.shell_available)
        self.assertEqual(runtime.health.failure_code, "recovery_failed")
        recovered = await runtime.audit.get_run(handle.run_id)  # type: ignore[union-attr]
        assert recovered.record.finalization is not None
        self.assertIs(
            recovered.record.finalization.outcome,
            TerminalOutcome.RECOVERY_FAILED,
        )

    async def test_verified_container_descriptor_builds_the_exact_backend(self):
        self.image_lock.write_text(
            json.dumps(
                {
                    "reference": DIGEST,
                    "digest": DIGEST,
                    "platform": "linux/amd64",
                    "user": "65532:65532",
                    "entrypoint_version": "1",
                }
            )
        )
        runtime_info = ContainerRuntimeInfo(
            name="docker",
            executable=Path("/usr/bin/docker"),
            client_version="test",
            server_version="test",
            daemon_reachable=True,
            rootless="unknown",
        )
        image = load_image_lock(self.image_lock)
        descriptor = BackendDescriptor(
            name="container",
            available=True,
            capabilities=ContainerBackendFacts(
                runtime="docker",
                runtime_version="test",
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
            ).capabilities(),
            version="test",
            runtime=runtime_info,
        )

        runtime = await bootstrap_execution(
            self.approvals,
            config=self.config(),
            discovery_snapshot=snapshot(
                container=descriptor,
                runtimes=(runtime_info,),
            ),
        )

        self.assertTrue(runtime.shell_available)
        self.assertEqual(runtime.backends[0].descriptor.name, "container")


class BootstrapSinkTests(BootstrapFixture):
    async def test_configured_sinks_reach_the_constructed_runner(self):
        events = CollectingEventSink()
        preview = PreviewCollector()

        runtime = await bootstrap_execution(
            self.approvals,
            config=self.config(event_sink=events, preview_sink=preview),
            discovery_snapshot=snapshot(posix=posix_descriptor()),
        )

        assert runtime.service is not None
        runner = runtime.service._runner
        assert runner is not None
        self.assertIs(runner._event_sink, events)
        self.assertIs(runner._preview_sink, preview)

    async def test_omitted_sinks_leave_a_null_event_sink(self):
        runtime = await bootstrap_execution(
            self.approvals,
            config=self.config(),
            discovery_snapshot=snapshot(posix=posix_descriptor()),
        )

        assert runtime.service is not None
        runner = runtime.service._runner
        assert runner is not None
        self.assertIsInstance(runner._event_sink, NullEventSink)
        self.assertIsNone(runner._preview_sink)

    def test_configuration_rejects_objects_that_are_not_sinks(self):
        with self.assertRaises(TypeError):
            self.config(event_sink=object())
        with self.assertRaises(TypeError):
            self.config(preview_sink=object())


if __name__ == "__main__":
    unittest.main()
