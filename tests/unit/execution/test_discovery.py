from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from truecoder.execution.discovery import (
    PROBE_OUTPUT_LIMIT_BYTES,
    PROBE_TIMEOUT_SECONDS,
    DiscoveryIO,
    ProbeResult,
    SystemDiscoveryIO,
    derive_backend_descriptors,
    discover_cgroup_v2,
    discover_container_runtimes,
    discover_execution_environment,
    discover_host,
    discover_shells,
)

ROOT = Path.cwd().resolve()
CGROUP_CONTROLLERS = Path("/sys/fs/cgroup/cgroup.controllers")
PROC_SELF_CGROUP = Path("/proc/self/cgroup")


class FakeDiscoveryIO:
    def __init__(
        self,
        *,
        system: str = "Linux",
        machine: str = "x86_64",
        release: str = "6.12",
    ) -> None:
        self.system = system
        self.machine = machine
        self.release = release
        self.executables: dict[str, Path] = {}
        self.files: dict[Path, str] = {}
        self.writable: set[Path] = set()
        self.probes: dict[tuple[str, ...], ProbeResult] = {}
        self.probe_calls: list[tuple[str, ...]] = []

    def platform_system(self) -> str:
        return self.system

    def platform_machine(self) -> str:
        return self.machine

    def platform_release(self) -> str:
        return self.release

    def which(self, executable: str) -> Path | None:
        return self.executables.get(executable)

    def path_exists(self, path: Path) -> bool:
        return path in self.files

    def path_writable(self, path: Path) -> bool:
        return path in self.writable

    def read_text(self, path: Path, maximum_bytes: int) -> str:
        value = self.files[path]
        if len(value.encode()) > maximum_bytes:
            raise ValueError("fake file exceeds limit")
        return value

    async def run_probe(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> ProbeResult:
        self.probe_calls.append(argv)
        self.assert_probe_bounds(timeout_seconds, max_output_bytes)
        return self.probes.get(
            argv,
            ProbeResult(
                status="failed",
                exit_code=1,
                diagnostic="unconfigured probe",
            ),
        )

    @staticmethod
    def assert_probe_bounds(
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> None:
        if timeout_seconds != PROBE_TIMEOUT_SECONDS:
            raise AssertionError("unexpected probe timeout")
        if max_output_bytes != PROBE_OUTPUT_LIMIT_BYTES:
            raise AssertionError("unexpected probe output limit")


class HostAndShellDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_maps_supported_and_unknown_host_systems(self):
        fixtures = (
            ("Linux", "linux", "posix"),
            ("Darwin", "macos", "posix"),
            ("Windows", "windows", "windows"),
            ("Plan9", "unknown", "unknown"),
        )
        for raw, expected_system, expected_family in fixtures:
            with self.subTest(raw=raw):
                host = discover_host(FakeDiscoveryIO(system=raw))
                self.assertEqual(host.system, expected_system)
                self.assertEqual(host.family, expected_family)

    async def test_discovers_and_deduplicates_posix_shells(self):
        io = FakeDiscoveryIO()
        io.executables.update(
            {
                "sh": ROOT / "bin" / "sh",
                "bash": ROOT / "bin" / "bash",
                "zsh": ROOT / "bin" / "bash",
                "pwsh": ROOT / "bin" / "pwsh",
            }
        )
        io.probes[(str(ROOT / "bin" / "bash"), "--version")] = ProbeResult(
            status="completed",
            exit_code=0,
            output="GNU bash, version 5.2.37",
        )
        io.probes[
            (
                str(ROOT / "bin" / "pwsh"),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$PSVersionTable.PSVersion.ToString()",
            )
        ] = ProbeResult(
            status="completed",
            exit_code=0,
            output="7.5.2",
        )

        shells = await discover_shells(discover_host(io), io)

        self.assertEqual(
            tuple(shell.name for shell in shells),
            ("sh", "bash", "pwsh"),
        )
        self.assertEqual(shells[1].version, "5.2.37")
        self.assertEqual(shells[2].shell_kind, "powershell")

    async def test_windows_discovers_cmd_without_claiming_supported_semantics(self):
        io = FakeDiscoveryIO(system="Windows", machine="AMD64")
        io.executables.update(
            {
                "powershell.exe": ROOT / "PowerShell.exe",
                "cmd.exe": ROOT / "cmd.exe",
            }
        )
        powershell_argv = (
            str(ROOT / "PowerShell.exe"),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        )
        cmd_argv = (str(ROOT / "cmd.exe"), "/d", "/c", "ver")
        io.probes[powershell_argv] = ProbeResult(
            status="completed",
            exit_code=0,
            output="5.1.26100.1",
        )
        io.probes[cmd_argv] = ProbeResult(
            status="completed",
            exit_code=0,
            output="Microsoft Windows [Version 10.0.26100.1]",
        )

        shells = await discover_shells(discover_host(io), io)

        self.assertEqual(tuple(shell.name for shell in shells), ("powershell", "cmd"))
        self.assertEqual(shells[0].shell_kind, "powershell")
        self.assertIsNone(shells[1].shell_kind)


class CgroupDiscoveryTests(unittest.TestCase):
    def test_non_linux_has_no_cgroup_snapshot(self):
        io = FakeDiscoveryIO(system="Darwin")
        self.assertIsNone(discover_cgroup_v2(discover_host(io), io))

    def test_missing_cgroup_v2_is_explicitly_unmounted(self):
        io = FakeDiscoveryIO()
        info = discover_cgroup_v2(discover_host(io), io)

        self.assertIsNotNone(info)
        self.assertFalse(info.mounted)  # type: ignore[union-attr]

    def test_discovers_controllers_and_delegated_writability(self):
        io = FakeDiscoveryIO()
        delegated = Path("/sys/fs/cgroup/user.slice/test.scope")
        io.files[CGROUP_CONTROLLERS] = "pids memory cpu io\n"
        io.files[PROC_SELF_CGROUP] = "0::/user.slice/test.scope\n"
        io.files[delegated / "cgroup.subtree_control"] = "memory pids cpu\n"
        io.writable.add(delegated)

        info = discover_cgroup_v2(discover_host(io), io)

        self.assertIsNotNone(info)
        self.assertTrue(info.writable)  # type: ignore[union-attr]
        self.assertEqual(
            info.controllers,  # type: ignore[union-attr]
            ("cpu", "io", "memory", "pids"),
        )
        self.assertEqual(
            info.enabled_controllers,  # type: ignore[union-attr]
            ("cpu", "memory", "pids"),
        )
        self.assertEqual(info.delegated_path, delegated)  # type: ignore[union-attr]

    def test_malformed_membership_falls_back_to_cgroup_root(self):
        io = FakeDiscoveryIO()
        root = Path("/sys/fs/cgroup")
        io.files[CGROUP_CONTROLLERS] = "cpu\n"
        io.files[PROC_SELF_CGROUP] = "0::/../../outside\n"
        io.writable.add(root)

        info = discover_cgroup_v2(discover_host(io), io)

        self.assertTrue(info.writable)  # type: ignore[union-attr]
        self.assertEqual(info.delegated_path, root)  # type: ignore[union-attr]


class RuntimeDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_docker_versions_and_rootless_mode(self):
        io = FakeDiscoveryIO()
        docker = ROOT / "docker"
        io.executables["docker"] = docker
        io.probes[(str(docker), "--version")] = ProbeResult(
            status="completed",
            exit_code=0,
            output="Docker version 28.1.1, build abc",
        )
        io.probes[(str(docker), "version", "--format", "{{json .}}")] = ProbeResult(
            status="completed",
            exit_code=0,
            output=json.dumps(
                {
                    "Client": {"Version": "28.1.1"},
                    "Server": {"Version": "28.1.0"},
                }
            ),
        )
        io.probes[
            (
                str(docker),
                "info",
                "--format",
                "{{json .SecurityOptions}}",
            )
        ] = ProbeResult(
            status="completed",
            exit_code=0,
            output='["name=seccomp","name=rootless"]',
        )

        runtimes = await discover_container_runtimes(io)

        self.assertEqual(len(runtimes), 1)
        self.assertEqual(runtimes[0].client_version, "28.1.1")
        self.assertEqual(runtimes[0].server_version, "28.1.0")
        self.assertEqual(runtimes[0].rootless, "yes")
        self.assertTrue(runtimes[0].daemon_reachable)

    async def test_distinguishes_installed_client_from_unreachable_daemon(self):
        io = FakeDiscoveryIO()
        podman = ROOT / "podman"
        io.executables["podman"] = podman
        io.probes[(str(podman), "--version")] = ProbeResult(
            status="completed",
            exit_code=0,
            output="podman version 5.5.0",
        )
        io.probes[(str(podman), "info", "--format", "json")] = ProbeResult(
            status="failed",
            exit_code=125,
            diagnostic="service unavailable",
        )

        runtime = (await discover_container_runtimes(io))[0]

        self.assertEqual(runtime.client_version, "5.5.0")
        self.assertFalse(runtime.daemon_reachable)
        self.assertEqual(runtime.rootless, "unknown")
        self.assertNotIn("service unavailable", repr(runtime))

    async def test_parses_podman_rootless_boolean(self):
        io = FakeDiscoveryIO()
        podman = ROOT / "podman"
        io.executables["podman"] = podman
        io.probes[(str(podman), "--version")] = ProbeResult(
            status="completed",
            exit_code=0,
            output="podman version 5.5.0",
        )
        io.probes[(str(podman), "info", "--format", "json")] = ProbeResult(
            status="completed",
            exit_code=0,
            output=json.dumps(
                {
                    "host": {"security": {"rootless": False}},
                    "version": {"Version": "5.5.0"},
                }
            ),
        )

        runtime = (await discover_container_runtimes(io))[0]

        self.assertTrue(runtime.daemon_reachable)
        self.assertEqual(runtime.rootless, "no")


class BackendDescriptorDerivationTests(unittest.IsolatedAsyncioTestCase):
    async def test_linux_capabilities_depend_on_delegated_controllers(self):
        io = FakeDiscoveryIO()
        io.executables["sh"] = ROOT / "sh"
        io.files[CGROUP_CONTROLLERS] = "cpu memory pids\n"
        io.files[PROC_SELF_CGROUP] = "0::/\n"
        io.files[Path("/sys/fs/cgroup/cgroup.subtree_control")] = (
            "cpu memory pids\n"
        )
        io.writable.add(Path("/sys/fs/cgroup"))

        snapshot = await discover_execution_environment(io)
        posix = snapshot.backend("posix")

        self.assertTrue(posix.available)
        self.assertEqual(posix.capabilities.memory_limits, "enforced")
        self.assertEqual(posix.capabilities.cpu_limits, "enforced")
        self.assertEqual(posix.capabilities.process_limits, "enforced")
        self.assertFalse(snapshot.backend("windows").available)
        self.assertFalse(snapshot.backend("container").available)

    async def test_windows_and_container_capabilities_are_independent(self):
        io = FakeDiscoveryIO(system="Windows", machine="AMD64")
        io.executables["powershell.exe"] = ROOT / "powershell.exe"
        io.executables["docker"] = ROOT / "docker.exe"
        powershell_argv = (
            str(ROOT / "powershell.exe"),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        )
        io.probes[powershell_argv] = ProbeResult(
            status="completed",
            exit_code=0,
            output="7.5.2",
        )
        docker = ROOT / "docker.exe"
        io.probes[(str(docker), "--version")] = ProbeResult(
            status="completed",
            exit_code=0,
            output="Docker version 28.0.0",
        )
        io.probes[(str(docker), "version", "--format", "{{json .}}")] = ProbeResult(
            status="completed",
            exit_code=0,
            output='{"Client":{"Version":"28"},"Server":{"Version":"28"}}',
        )
        io.probes[
            (
                str(docker),
                "info",
                "--format",
                "{{json .SecurityOptions}}",
            )
        ] = ProbeResult(
            status="completed",
            exit_code=0,
            output="[]",
        )

        snapshot = await discover_execution_environment(io)

        self.assertTrue(snapshot.backend("windows").available)
        self.assertFalse(snapshot.backend("posix").available)
        self.assertTrue(snapshot.backend("container").available)
        self.assertEqual(
            snapshot.backend("container").capabilities.network_isolation,
            "enforced",
        )

    def test_runtime_inspection_failure_is_an_explicit_unavailable_reason(self):
        host = discover_host(FakeDiscoveryIO())
        from truecoder.execution.backends.models import ContainerRuntimeInfo

        broken = ContainerRuntimeInfo(
            name="docker",
            executable=ROOT / "docker",
            client_version="28",
            server_version=None,
            daemon_reachable=True,
            rootless="unknown",
            diagnostic="invalid metadata",
        )

        descriptors = derive_backend_descriptors(
            host=host,
            shells=(),
            cgroup_v2=None,
            runtimes=(broken,),
        )
        container = next(item for item in descriptors if item.name == "container")

        self.assertFalse(container.available)
        self.assertEqual(
            container.unavailable_reasons[0].code,
            "container-runtime-inspection-failed",
        )


class SystemDiscoveryIOTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_probe_success_failure_timeout_and_output_limit(self):
        io: DiscoveryIO = SystemDiscoveryIO()
        executable = str(Path(sys.executable).resolve())

        success = await io.run_probe(
            (executable, "-c", "print('ok')"),
            timeout_seconds=1,
            max_output_bytes=128,
        )
        failure = await io.run_probe(
            (executable, "-c", "raise SystemExit(7)"),
            timeout_seconds=1,
            max_output_bytes=128,
        )
        timeout = await io.run_probe(
            (executable, "-c", "import time; time.sleep(1)"),
            timeout_seconds=0.05,
            max_output_bytes=128,
        )
        output_limit = await io.run_probe(
            (executable, "-c", "print('x' * 10000)"),
            timeout_seconds=1,
            max_output_bytes=64,
        )

        self.assertEqual(success.status, "completed")
        self.assertEqual(success.output, "ok")
        self.assertEqual(failure.status, "failed")
        self.assertEqual(failure.exit_code, 7)
        self.assertEqual(timeout.status, "timed_out")
        self.assertEqual(output_limit.status, "output_limit")
        self.assertLessEqual(len(output_limit.output.encode()), 64)


if __name__ == "__main__":
    unittest.main()
