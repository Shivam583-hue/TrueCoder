from __future__ import annotations

import unittest
from pathlib import Path

from truecoder.execution.backends.models import (
    BackendDescriptor,
    CgroupV2Info,
    ContainerRuntimeInfo,
    DiscoverySnapshot,
    HostPlatformInfo,
    UnavailableReason,
)
from truecoder.execution.errors import (
    BackendSelectionError,
    NoCompatibleBackendError,
)
from truecoder.execution.models import (
    BackendCapabilities,
    CapabilityRequirements,
    ExecutionLimits,
    ExecutionRequest,
    PolicyDecision,
    PolicyReason,
    RiskLevel,
)
from truecoder.execution.selection import (
    capability_meets,
    check_backend,
    select_backend,
)

ROOT = Path.cwd().resolve()


def limits() -> ExecutionLimits:
    return ExecutionLimits(
        timeout_seconds=30,
        max_output_bytes=1024,
        max_return_bytes=512,
        memory_bytes=256 * 1024 * 1024,
        cpu_seconds=10,
        max_processes=32,
        termination_grace_seconds=1,
    )


def request(**overrides: object) -> ExecutionRequest:
    values: dict[str, object] = {
        "mode": "exec",
        "argv": ("pytest", "-q"),
        "script": None,
        "working_directory": ROOT,
        "limits": limits(),
        "network_access": False,
        "filesystem_mode": "workspace-read",
        "backend": "auto",
        "shell_kind": "auto",
        "environment": (),
        "require_cancellation": True,
    }
    values.update(overrides)
    return ExecutionRequest(**values)  # type: ignore[arg-type]


def decision(**overrides: object) -> PolicyDecision:
    values: dict[str, object] = {
        "allowed": True,
        "risk": RiskLevel.LOW,
        "requires_approval": False,
        "effective_limits": limits(),
        "requirements": CapabilityRequirements(
            filesystem_isolation="enforced",
            network_isolation="enforced",
            memory_limits="enforced",
            cpu_limits="enforced",
            process_limits="enforced",
            timeout_enforcement="enforced",
            cancellation="enforced",
        ),
        "reasons": (),
    }
    values.update(overrides)
    return PolicyDecision(**values)  # type: ignore[arg-type]


def capabilities(**overrides: object) -> BackendCapabilities:
    values: dict[str, object] = {
        "filesystem_isolation": "enforced",
        "network_isolation": "enforced",
        "memory_limits": "enforced",
        "cpu_limits": "enforced",
        "process_limits": "enforced",
        "timeout_enforcement": "enforced",
        "cancellation": "enforced",
        "supported_execution_modes": ("exec", "shell"),
        "supported_filesystem_modes": (
            "host",
            "workspace-read",
            "workspace-write",
        ),
        "supported_shells": ("posix",),
    }
    values.update(overrides)
    return BackendCapabilities(**values)  # type: ignore[arg-type]


def runtime() -> ContainerRuntimeInfo:
    return ContainerRuntimeInfo(
        name="docker",
        executable=ROOT / "docker",
        client_version="28",
        server_version="28",
        daemon_reachable=True,
        rootless="yes",
    )


def descriptor(
    name: str,
    **overrides: object,
) -> BackendDescriptor:
    values: dict[str, object] = {
        "name": name,
        "available": True,
        "capabilities": capabilities(),
        "version": "1",
        "runtime": runtime() if name == "container" else None,
        "unavailable_reasons": (),
    }
    values.update(overrides)
    return BackendDescriptor(**values)  # type: ignore[arg-type]


def snapshot(
    *,
    family: str = "posix",
    posix: BackendDescriptor | None = None,
    windows: BackendDescriptor | None = None,
    container: BackendDescriptor | None = None,
) -> DiscoverySnapshot:
    system = (
        "linux"
        if family == "posix"
        else "windows"
        if family == "windows"
        else "unknown"
    )
    wrong_host = UnavailableReason(
        code="wrong-host",
        message="Backend does not match the host.",
    )
    return DiscoverySnapshot(
        host=HostPlatformInfo(
            system=system,  # type: ignore[arg-type]
            family=family,  # type: ignore[arg-type]
            architecture="test",
        ),
        shells=(),
        cgroup_v2=(
            CgroupV2Info(
                mounted=True,
                writable=True,
                controllers=("cpu", "memory", "pids"),
                enabled_controllers=("cpu", "memory", "pids"),
                delegated_path=ROOT / "cgroup",
            )
            if system == "linux"
            else None
        ),
        runtimes=(runtime(),),
        backends=(
            posix
            or descriptor(
                "posix",
                available=family == "posix",
                version="1" if family == "posix" else None,
                runtime=None,
                unavailable_reasons=() if family == "posix" else (wrong_host,),
            ),
            windows
            or descriptor(
                "windows",
                available=family == "windows",
                version="1" if family == "windows" else None,
                runtime=None,
                unavailable_reasons=() if family == "windows" else (wrong_host,),
            ),
            container or descriptor("container"),
        ),
    )


class CapabilityTruthTableTests(unittest.TestCase):
    def test_complete_capability_truth_table(self):
        expected = {
            "unsupported": {
                "none": True,
                "best_effort": False,
                "enforced": False,
            },
            "best_effort": {
                "none": True,
                "best_effort": True,
                "enforced": False,
            },
            "enforced": {
                "none": True,
                "best_effort": True,
                "enforced": True,
            },
        }

        for actual, requirements in expected.items():
            for required, result in requirements.items():
                with self.subTest(actual=actual, required=required):
                    self.assertEqual(
                        capability_meets(
                            actual,  # type: ignore[arg-type]
                            required,  # type: ignore[arg-type]
                        ),
                        result,
                    )

    def test_rejects_unknown_levels(self):
        with self.assertRaises(ValueError):
            capability_meets("sometimes", "none")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            capability_meets("enforced", "sometimes")  # type: ignore[arg-type]


class BackendCompatibilityTests(unittest.TestCase):
    def test_matches_each_capability_requirement_independently(self):
        fields = (
            ("filesystem_isolation", "filesystem-isolation-insufficient"),
            ("network_isolation", "network-isolation-insufficient"),
            ("memory_limits", "memory-limits-insufficient"),
            ("cpu_limits", "cpu-limits-insufficient"),
            ("process_limits", "process-limits-insufficient"),
            ("timeout_enforcement", "timeout-enforcement-insufficient"),
            ("cancellation", "cancellation-insufficient"),
        )
        for field_name, reason_code in fields:
            weak = descriptor(
                "container",
                capabilities=capabilities(**{field_name: "best_effort"}),
            )
            with self.subTest(field=field_name):
                result = check_backend(weak, request(), decision())
                self.assertFalse(result.compatible)
                self.assertIn(
                    reason_code,
                    tuple(reason.code for reason in result.reasons),
                )

    def test_reports_mode_filesystem_and_shell_failures_together(self):
        backend = descriptor(
            "container",
            capabilities=capabilities(
                supported_execution_modes=("exec",),
                supported_filesystem_modes=("host",),
                supported_shells=(),
            ),
        )
        shell_request = request(
            mode="shell",
            argv=None,
            script="pytest -q",
            shell_kind="powershell",
        )

        result = check_backend(backend, shell_request, decision())

        self.assertEqual(
            tuple(reason.code for reason in result.reasons),
            (
                "execution-mode-unsupported",
                "filesystem-mode-unsupported",
                "shell-unsupported",
            ),
        )

    def test_explicit_shell_is_never_substituted(self):
        backend = descriptor("container")
        shell_request = request(
            mode="shell",
            argv=None,
            script="echo hi",
            shell_kind="powershell",
        )

        result = check_backend(backend, shell_request, decision())

        self.assertFalse(result.compatible)
        self.assertIsNone(result.resolved_shell)
        self.assertEqual(result.reasons[0].code, "shell-unsupported")

    def test_auto_shell_resolution_is_deterministic(self):
        backend = descriptor(
            "posix",
            runtime=None,
            capabilities=capabilities(
                supported_shells=("powershell", "posix"),
            ),
        )
        shell_request = request(
            mode="shell",
            argv=None,
            script="echo hi",
            shell_kind="auto",
        )

        first = check_backend(backend, shell_request, decision())
        second = check_backend(backend, shell_request, decision())

        self.assertEqual(first, second)
        self.assertEqual(first.resolved_shell, "posix")

    def test_unavailable_backend_returns_discovery_reasons_only(self):
        reason = UnavailableReason(
            code="daemon-down",
            message="Container daemon is unavailable.",
        )
        unavailable = descriptor(
            "container",
            available=False,
            version=None,
            runtime=None,
            unavailable_reasons=(reason,),
        )

        result = check_backend(unavailable, request(), decision())

        self.assertFalse(result.compatible)
        self.assertEqual(result.reasons, (reason,))

    def test_denied_policy_cannot_reach_selection(self):
        denied = decision(
            allowed=False,
            risk=RiskLevel.CRITICAL,
            reasons=(
                PolicyReason(
                    code="denied",
                    message="Denied.",
                    rule_id="test.denied",
                ),
            ),
        )

        with self.assertRaises(BackendSelectionError):
            check_backend(descriptor("container"), request(), denied)


class BackendSelectionTests(unittest.TestCase):
    def test_auto_prefers_compatible_local_backend(self):
        host_request = request(
            filesystem_mode="host",
            network_access=True,
        )
        no_isolation = decision(
            requirements=CapabilityRequirements(
                memory_limits="enforced",
                cpu_limits="enforced",
                process_limits="enforced",
                timeout_enforcement="enforced",
                cancellation="enforced",
            )
        )

        selected = select_backend(host_request, no_isolation, snapshot())

        self.assertEqual(selected.descriptor.name, "posix")
        self.assertIn("first compatible", selected.selection_reason)

    def test_auto_uses_container_only_after_local_is_rejected(self):
        local = descriptor(
            "posix",
            runtime=None,
            capabilities=capabilities(
                filesystem_isolation="unsupported",
                network_isolation="unsupported",
            ),
        )

        selected = select_backend(
            request(),
            decision(),
            snapshot(posix=local),
        )

        self.assertEqual(selected.descriptor.name, "container")
        self.assertIn("posix", selected.selection_reason)

    def test_explicit_local_never_falls_back_to_container(self):
        local = descriptor(
            "posix",
            runtime=None,
            capabilities=capabilities(
                filesystem_isolation="unsupported",
                network_isolation="unsupported",
            ),
        )

        with self.assertRaises(NoCompatibleBackendError) as caught:
            select_backend(
                request(backend="local"),
                decision(),
                snapshot(posix=local),
            )

        self.assertEqual(
            tuple(name for name, _reasons in caught.exception.failures),
            ("posix",),
        )

    def test_explicit_container_never_falls_back_to_local(self):
        reason = UnavailableReason(
            code="daemon-down",
            message="Container daemon is unavailable.",
        )
        unavailable = descriptor(
            "container",
            available=False,
            version=None,
            runtime=None,
            unavailable_reasons=(reason,),
        )

        with self.assertRaises(NoCompatibleBackendError) as caught:
            select_backend(
                request(backend="container"),
                decision(),
                snapshot(container=unavailable),
            )

        self.assertEqual(
            tuple(name for name, _reasons in caught.exception.failures),
            ("container",),
        )
        self.assertIn(
            "Container daemon is unavailable.",
            caught.exception.failures[0][1],
        )

    def test_unknown_host_reports_every_permitted_candidate(self):
        incompatible_container = descriptor(
            "container",
            capabilities=capabilities(network_isolation="unsupported"),
        )

        with self.assertRaises(NoCompatibleBackendError) as caught:
            select_backend(
                request(),
                decision(),
                snapshot(family="unknown", container=incompatible_container),
            )

        self.assertEqual(
            tuple(name for name, _reasons in caught.exception.failures),
            ("posix", "windows", "container"),
        )

    def test_selection_does_not_mutate_any_input(self):
        invocation = request()
        policy = decision()
        discovered = snapshot(
            posix=descriptor(
                "posix",
                runtime=None,
                capabilities=capabilities(
                    filesystem_isolation="unsupported",
                    network_isolation="unsupported",
                ),
            )
        )
        originals = invocation, policy, discovered

        first = select_backend(invocation, policy, discovered)
        second = select_backend(invocation, policy, discovered)

        self.assertEqual(first, second)
        self.assertEqual((invocation, policy, discovered), originals)


if __name__ == "__main__":
    unittest.main()
