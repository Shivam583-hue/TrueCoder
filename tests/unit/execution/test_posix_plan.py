from __future__ import annotations

from tests.helpers.platforms import skip_module_on_windows

skip_module_on_windows('POSIX launch planning')

import unittest
from dataclasses import replace
from pathlib import Path

from truecoder.execution.backends.models import (
    BackendDescriptor,
    DiscoveredProgram,
    SelectedBackend,
)
from truecoder.execution.backends.posix_plan import (
    POSIX_PROTOCOL_VERSION,
    build_posix_launch_plan,
    plan_from_payload,
    plan_to_payload,
)
from truecoder.execution.environment import construct_environment
from truecoder.execution.models import (
    BackendCapabilities,
    ExecutionLimits,
    ExecutionRequest,
)

ROOT = Path.cwd().resolve()


def _limits() -> ExecutionLimits:
    return ExecutionLimits(
        timeout_seconds=5,
        max_output_bytes=4096,
        max_return_bytes=1024,
        memory_bytes=1024 * 1024,
        cpu_seconds=2.5,
        max_processes=8,
    )


def _request(**overrides: object) -> ExecutionRequest:
    values: dict[str, object] = {
        "mode": "exec",
        "argv": ("python", "a b", "", '"quoted"', "λ"),
        "script": None,
        "working_directory": ROOT,
        "limits": _limits(),
        "network_access": True,
        "filesystem_mode": "host",
    }
    values.update(overrides)
    return ExecutionRequest(**values)  # type: ignore[arg-type]


def _selected(
    resolved_shell: str | None = None,
) -> SelectedBackend:
    return SelectedBackend(
        descriptor=BackendDescriptor(
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
                supported_shells=("posix", "powershell"),
            ),
            version="test",
        ),
        resolved_shell=resolved_shell,  # type: ignore[arg-type]
        selection_reason="test selection",
    )


def _environment():
    return construct_environment(
        platform="posix",
        inherited={"PATH": "/usr/bin", "OPENAI_API_KEY": "hidden"},
        requested=(("MODE", "test"),),
    )


class PosixLaunchPlanTests(unittest.TestCase):
    def test_exec_preserves_every_argument_and_hides_environment_repr(self):
        request = _request()

        plan = build_posix_launch_plan(
            request,
            _selected(),
            _environment(),
            (),
            execution_id="exec_plan",
        )

        self.assertEqual(plan.argv, request.argv)
        self.assertNotIn("/usr/bin", repr(plan))
        self.assertNotIn("hidden", repr(plan))

    def test_posix_shell_uses_canonical_path_and_one_script_argument(self):
        request = _request(
            mode="shell",
            argv=None,
            script="printf '%s' \"$HOME\" | sed s/x/y/",
            shell_kind="posix",
        )
        shell = DiscoveredProgram(
            name="sh",
            path=ROOT / "bin" / ".." / "sh",
            shell_kind="posix",
        )

        plan = build_posix_launch_plan(
            request,
            _selected("posix"),
            _environment(),
            (shell,),
            execution_id="exec_shell",
        )

        self.assertEqual(plan.argv, (str(ROOT / "sh"), "-lc", request.script))

    def test_powershell_uses_noninteractive_argv(self):
        request = _request(
            mode="shell",
            argv=None,
            script="Get-ChildItem",
            shell_kind="powershell",
        )
        shell = DiscoveredProgram(
            name="pwsh",
            path=ROOT / "pwsh",
            shell_kind="powershell",
        )

        plan = build_posix_launch_plan(
            request,
            _selected("powershell"),
            _environment(),
            (shell,),
            execution_id="exec_pwsh",
        )

        self.assertEqual(
            plan.argv,
            (
                str(ROOT / "pwsh"),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-ChildItem",
            ),
        )

    def test_payload_round_trip_is_exact(self):
        plan = build_posix_launch_plan(
            _request(),
            _selected(),
            _environment(),
            (),
            execution_id="exec_wire",
            cgroup_path=ROOT / "cgroup" / "exec",
            cgroup_controllers=("cpu", "memory", "pids"),
        )

        decoded = plan_from_payload(plan_to_payload(plan))

        self.assertEqual(decoded, plan)
        self.assertEqual(decoded.protocol_version, POSIX_PROTOCOL_VERSION)

    def test_payload_rejects_unknown_fields_and_wrong_version(self):
        plan = build_posix_launch_plan(
            _request(),
            _selected(),
            _environment(),
            (),
            execution_id="exec_invalid",
        )
        payload = plan_to_payload(plan)
        payload["extra"] = True
        with self.assertRaisesRegex(ValueError, "fields"):
            plan_from_payload(payload)
        with self.assertRaisesRegex(ValueError, "version"):
            replace(plan, protocol_version=99)

    def test_invalid_environment_never_becomes_a_plan(self):
        environment = construct_environment(
            platform="posix",
            inherited={"PATH": "/usr/bin"},
            requested=(("GITHUB_TOKEN", "secret"),),
        )

        with self.assertRaisesRegex(ValueError, "violations"):
            build_posix_launch_plan(
                _request(),
                _selected(),
                environment,
                (),
                execution_id="exec_bad_env",
            )


if __name__ == "__main__":
    unittest.main()
