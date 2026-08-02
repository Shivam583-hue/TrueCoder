from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from truecoder.execution.backends.models import (
    BACKEND_NAMES,
    BackendCompatibility,
    BackendDescriptor,
    BackendExit,
    BackendOutputChunk,
    CgroupV2Info,
    CleanupResult,
    ContainerRuntimeInfo,
    DiscoveredProgram,
    DiscoverySnapshot,
    HostPlatformInfo,
    SelectedBackend,
    UnavailableReason,
)
from truecoder.execution.models import BackendCapabilities, NativeDiagnostic

ROOT = Path.cwd().resolve()


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
        "supported_filesystem_modes": ("workspace-read", "workspace-write"),
        "supported_shells": ("posix",),
    }
    values.update(overrides)
    return BackendCapabilities(**values)  # type: ignore[arg-type]


def runtime(**overrides: object) -> ContainerRuntimeInfo:
    values: dict[str, object] = {
        "name": "docker",
        "executable": ROOT / "docker",
        "client_version": "28.0.0",
        "server_version": "28.0.0",
        "daemon_reachable": True,
        "rootless": "yes",
        "diagnostic": None,
    }
    values.update(overrides)
    return ContainerRuntimeInfo(**values)  # type: ignore[arg-type]


def descriptor(
    name: str = "container",
    **overrides: object,
) -> BackendDescriptor:
    values: dict[str, object] = {
        "name": name,
        "available": True,
        "capabilities": capabilities(),
        "version": "28.0.0",
        "runtime": runtime() if name == "container" else None,
        "unavailable_reasons": (),
    }
    values.update(overrides)
    return BackendDescriptor(**values)  # type: ignore[arg-type]


class DiscoveryModelTests(unittest.TestCase):
    def test_models_are_immutable_and_hide_diagnostics_and_bytes(self):
        host = HostPlatformInfo(
            system="linux",
            family="posix",
            architecture="x86_64",
            release="6.12",
        )
        reason = UnavailableReason(
            code="runtime-unreachable",
            message="Runtime is unavailable.",
            diagnostic="private native detail",
        )
        chunk = BackendOutputChunk(stream="stdout", data=b"secret bytes")

        with self.assertRaises(FrozenInstanceError):
            host.system = "windows"  # type: ignore[misc]
        self.assertNotIn("private native detail", repr(reason))
        self.assertNotIn("secret bytes", repr(chunk))

    def test_host_system_and_family_must_agree(self):
        with self.assertRaisesRegex(ValueError, "requires family"):
            HostPlatformInfo(
                system="windows",
                family="posix",
                architecture="amd64",
            )

    def test_program_paths_are_absolute_and_shell_kind_is_valid(self):
        program = DiscoveredProgram(
            name="bash",
            path=ROOT / "bin" / ".." / "bash",
            shell_kind="posix",
            version="5.2",
        )

        self.assertEqual(program.path, ROOT / "bash")
        with self.assertRaises(ValueError):
            replace(program, path=Path("relative"))
        with self.assertRaises(ValueError):
            replace(program, shell_kind="cmd")  # type: ignore[arg-type]

    def test_cgroup_state_cannot_claim_facts_when_unmounted(self):
        self.assertEqual(
            CgroupV2Info(
                mounted=True,
                writable=True,
                controllers=("cpu", "memory", "pids"),
                enabled_controllers=("cpu", "memory", "pids"),
                delegated_path=ROOT / "cgroup",
            ).controllers,
            ("cpu", "memory", "pids"),
        )
        with self.assertRaises(ValueError):
            CgroupV2Info(
                mounted=False,
                writable=True,
                controllers=(),
            )
        with self.assertRaisesRegex(ValueError, "sorted"):
            CgroupV2Info(
                mounted=True,
                writable=False,
                controllers=("memory", "cpu"),
            )

    def test_unreachable_runtime_has_conservative_server_facts(self):
        unreachable = runtime(
            server_version=None,
            daemon_reachable=False,
            rootless="unknown",
            diagnostic="daemon unavailable",
        )

        self.assertFalse(unreachable.daemon_reachable)
        self.assertNotIn("daemon unavailable", repr(unreachable))
        with self.assertRaises(ValueError):
            replace(unreachable, rootless="no")
        with self.assertRaises(ValueError):
            replace(unreachable, server_version="28")

    def test_backend_availability_and_runtime_invariants(self):
        reason = UnavailableReason(
            code="not-found",
            message="Backend was not found.",
        )
        unavailable = descriptor(
            available=False,
            runtime=None,
            version=None,
            unavailable_reasons=(reason,),
        )

        self.assertFalse(unavailable.available)
        with self.assertRaises(ValueError):
            replace(unavailable, unavailable_reasons=())
        with self.assertRaises(ValueError):
            replace(descriptor("posix"), runtime=runtime())
        with self.assertRaises(ValueError):
            descriptor(runtime=None)

    def test_snapshot_describes_every_backend_once(self):
        reason = UnavailableReason(
            code="wrong-host",
            message="Backend does not match this host.",
        )
        unavailable_capabilities = capabilities(
            filesystem_isolation="unsupported",
            network_isolation="unsupported",
            supported_execution_modes=("exec",),
            supported_filesystem_modes=("host",),
            supported_shells=(),
        )
        backends = (
            descriptor(
                "posix",
                capabilities=unavailable_capabilities,
                runtime=None,
            ),
            descriptor(
                "windows",
                available=False,
                capabilities=unavailable_capabilities,
                version=None,
                runtime=None,
                unavailable_reasons=(reason,),
            ),
            descriptor(),
        )
        snapshot = DiscoverySnapshot(
            host=HostPlatformInfo(
                system="linux",
                family="posix",
                architecture="x86_64",
            ),
            shells=(
                DiscoveredProgram(
                    name="sh",
                    path=ROOT / "sh",
                    shell_kind="posix",
                ),
            ),
            cgroup_v2=CgroupV2Info(
                mounted=True,
                writable=False,
                controllers=("cpu",),
                enabled_controllers=(),
                delegated_path=ROOT / "cgroup",
            ),
            runtimes=(runtime(),),
            backends=backends,
        )

        self.assertEqual(
            frozenset(backend.name for backend in snapshot.backends),
            BACKEND_NAMES,
        )
        self.assertEqual(snapshot.backend("container").name, "container")
        with self.assertRaises(ValueError):
            replace(snapshot, backends=backends[:-1])

    def test_compatibility_and_selection_are_consistent(self):
        compatible = BackendCompatibility(
            backend="container",
            compatible=True,
            resolved_shell="posix",
        )
        selected = SelectedBackend(
            descriptor=descriptor(),
            resolved_shell="posix",
            selection_reason="Only compatible isolated backend.",
        )

        self.assertTrue(compatible.compatible)
        self.assertEqual(selected.descriptor.name, "container")
        with self.assertRaises(ValueError):
            replace(compatible, compatible=False)
        with self.assertRaises(ValueError):
            replace(selected, descriptor=replace(descriptor(), available=False))


class BackendLifecycleValueTests(unittest.TestCase):
    def test_output_chunks_are_nonempty_bounded_bytes(self):
        self.assertEqual(
            BackendOutputChunk(stream="stderr", data=b"warning").data,
            b"warning",
        )
        for data in ("text", b"", b"x" * (64 * 1024 + 1)):
            with (
                self.subTest(data_type=type(data).__name__, size=len(data)),
                self.assertRaises((TypeError, ValueError)),
            ):
                BackendOutputChunk(stream="stdout", data=data)  # type: ignore[arg-type]

    def test_backend_exit_requires_a_native_reason_without_exit_code(self):
        self.assertEqual(BackendExit(exit_code=0).exit_code, 0)
        self.assertIsNone(
            BackendExit(exit_code=None, native_reason="signal 9").exit_code
        )
        with self.assertRaises(ValueError):
            BackendExit(exit_code=None)

    def test_cleanup_result_has_exact_diagnostic_semantics(self):
        diagnostic = NativeDiagnostic(
            code="close-failed",
            message="Could not close native handle.",
            platform="windows",
        )

        self.assertTrue(CleanupResult(complete=True).complete)
        self.assertFalse(CleanupResult(complete=False, diagnostic=diagnostic).complete)
        with self.assertRaises(ValueError):
            CleanupResult(complete=False)
        with self.assertRaises(ValueError):
            CleanupResult(complete=True, diagnostic=diagnostic)


if __name__ == "__main__":
    unittest.main()
