from __future__ import annotations

import unittest
from datetime import UTC, datetime

from tests.fakes.execution import AuditSpy, ScriptedBackend
from tests.unit.execution.test_runner_races import (
    ROOT,
    context,
    descriptor,
    request,
)
from truecoder.execution.audit.models import TerminalOutcome
from truecoder.execution.audit.service import AuditService
from truecoder.execution.backends.models import (
    BackendDescriptor,
    DiscoverySnapshot,
    HostPlatformInfo,
    UnavailableReason,
)
from truecoder.execution.backends.registry import BackendRegistry
from truecoder.execution.environment import EnvironmentPolicy
from truecoder.execution.models import (
    BackendCapabilities,
    ExecutionLimits,
    ExecutionRequest,
    RiskLevel,
)
from truecoder.execution.policy import PolicyConfig
from truecoder.execution.registry import ExecutionRegistry
from truecoder.execution.runner import ExecutionRunner
from truecoder.execution.service import ExecutionService

HOST_ENVIRONMENT = {"PATH": "/usr/bin", "LANG": "C.UTF-8"}


async def approve_execution(_prepared, _decision, _context) -> bool:
    return True


def unavailable(name) -> BackendDescriptor:
    return BackendDescriptor(
        name=name,
        available=False,
        capabilities=BackendCapabilities(
            filesystem_isolation="unsupported",
            network_isolation="unsupported",
            memory_limits="unsupported",
            cpu_limits="unsupported",
            process_limits="unsupported",
            timeout_enforcement="unsupported",
            cancellation="unsupported",
            supported_execution_modes=("exec",),
            supported_filesystem_modes=("host",),
            supported_shells=(),
        ),
        unavailable_reasons=(
            UnavailableReason(
                code="not-present",
                message="This backend is not present on the host.",
            ),
        ),
    )


def snapshot(*, posix_available: bool = True) -> DiscoverySnapshot:
    return DiscoverySnapshot(
        host=HostPlatformInfo(
            system="linux",
            family="posix",
            architecture="test",
        ),
        shells=(),
        cgroup_v2=None,
        runtimes=(),
        backends=(
            descriptor() if posix_available else unavailable("posix"),
            unavailable("windows"),
            unavailable("container"),
        ),
    )


def policy_config(*, ceiling_seconds: float = 600.0) -> PolicyConfig:
    return PolicyConfig(
        version="test-policy",
        limit_ceiling=ExecutionLimits(
            timeout_seconds=ceiling_seconds,
            max_output_bytes=1024 * 1024,
            max_return_bytes=64 * 1024,
        ),
        minimum_isolation="enforced",
        limit_enforcement="enforced",
        unknown_risk=RiskLevel.LOW,
    )


class ExecuteLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.spy = AuditSpy()
        self.audit = AuditService(
            self.spy,
            run_id_factory=lambda: self.spy.run_id,
            clock=lambda: datetime.now(UTC),
        )
        self.registry = ExecutionRegistry()
        self.backend = ScriptedBackend(descriptor(), handle_options={"exit_code": 0})

    def service(self, **overrides) -> ExecutionService:
        runner = ExecutionRunner(
            self.audit,
            BackendRegistry((self.backend,)),
            registry=self.registry,
            approval_gate=overrides.pop("approval_gate", approve_execution),
            safety_deadline_seconds=0.5,
        )
        return ExecutionService(
            self.registry,
            runner=runner,
            audit=self.audit,
            policy_config=overrides.pop("policy_config", policy_config()),
            discovery=overrides.pop("discovery", snapshot()),
            environment_policy=EnvironmentPolicy(),
            host_environment=HOST_ENVIRONMENT,
        )

    async def test_execute_runs_the_whole_lifecycle_from_a_request(self):
        service = self.service()

        result = await service.execute(request(), context())

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.backend, "posix")
        self.assertEqual(result.audit_id, self.spy.run_id)
        self.assertEqual(self.backend.start_count, 1)
        self.assertEqual(await self.registry.active_execution_ids(), ())

    async def test_execute_prepares_the_launch_the_backend_receives(self):
        service = self.service()

        await service.execute(request(), context())

        prepared = self.backend.prepared_seen[0]
        self.assertEqual(prepared.backend.name, "posix")
        self.assertEqual(
            dict(prepared.environment.variables).get("PATH"),
            "/usr/bin",
        )

    async def test_execute_applies_the_policy_limit_ceiling(self):
        service = self.service(policy_config=policy_config(ceiling_seconds=5.0))

        await service.execute(request(), context())

        prepared = self.backend.prepared_seen[0]
        self.assertEqual(prepared.request.limits.timeout_seconds, 5.0)

    async def test_execute_denies_without_selecting_a_backend(self):
        service = self.service()
        base = request()
        blocked = ExecutionRequest(
            mode="exec",
            argv=("python", "-V"),
            script=None,
            working_directory=ROOT,
            limits=base.limits,
            network_access=True,
            filesystem_mode="host",
            environment=(("OPENAI_API_KEY", "secret"),),
        )

        result = await service.execute(blocked, context())

        self.assertEqual(result.status, "denied")
        self.assertIsNone(result.backend)
        self.assertEqual(self.backend.start_count, 0)
        assert self.spy.finalization is not None
        self.assertIs(
            self.spy.finalization.outcome,
            TerminalOutcome.POLICY_DENIED,
        )

    async def test_execute_refuses_when_no_backend_is_available(self):
        service = self.service(discovery=snapshot(posix_available=False))

        result = await service.execute(request(), context())

        self.assertEqual(result.status, "failed_to_start")
        self.assertEqual(self.backend.start_count, 0)
        assert self.spy.finalization is not None
        self.assertIs(
            self.spy.finalization.outcome,
            TerminalOutcome.FAILED_TO_START,
        )
        self.assertEqual(self.spy.finalization.detail, "backend_unavailable")
        self.assertEqual(await self.registry.active_execution_ids(), ())

    async def test_execute_accepts_a_discovery_provider(self):
        calls: list[int] = []

        def provider() -> DiscoverySnapshot:
            calls.append(1)
            return snapshot()

        service = self.service(discovery=provider)

        result = await service.execute(request(), context())

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 1)

    async def test_execute_requires_its_pure_inputs(self):
        runner = ExecutionRunner(
            self.audit,
            BackendRegistry((self.backend,)),
            registry=self.registry,
        )

        without_policy = ExecutionService(self.registry, runner=runner)
        with self.assertRaises(RuntimeError):
            await without_policy.execute(request(), context())

        without_discovery = ExecutionService(
            self.registry,
            runner=runner,
            policy_config=policy_config(),
        )
        with self.assertRaises(RuntimeError):
            await without_discovery.execute(request(), context())

    async def test_run_prepared_remains_the_lower_level_entry_point(self):
        from tests.unit.execution.test_runner_races import decision, prepared

        service = self.service()

        result = await service.run_prepared(prepared(), decision(), context())

        self.assertEqual(result.status, "completed")


if __name__ == "__main__":
    unittest.main()
