from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from truecoder.execution.models import ExecutionLimits, ExecutionResult
from truecoder.tools.base import ToolExecutionError
from truecoder.tools.builtin.shell import (
    ShellArguments,
    ShellDefaults,
    build_shell_request,
    format_shell_result,
)


class ShellArgumentsTests(unittest.TestCase):
    def test_exec_mode_prefers_exact_argv(self):
        arguments = ShellArguments(argv=("pytest", "-q"))

        self.assertEqual(arguments.mode, "exec")
        self.assertEqual(arguments.argv, ("pytest", "-q"))
        self.assertIsNone(arguments.script)

    def test_exec_mode_rejects_script_or_explicit_shell(self):
        for values in (
            {"argv": ("echo",), "script": "echo"},
            {"argv": ("echo",), "shell_kind": "posix"},
            {"argv": ()},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ShellArguments(**values)

    def test_shell_mode_requires_only_script(self):
        valid = ShellArguments(mode="shell", script="pytest -q | tee report")
        self.assertEqual(valid.script, "pytest -q | tee report")

        for values in (
            {"mode": "shell"},
            {"mode": "shell", "script": " "},
            {"mode": "shell", "script": "echo", "argv": ("echo",)},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ShellArguments(**values)

    def test_return_limit_cannot_exceed_output_limit(self):
        with self.assertRaises(ValidationError):
            ShellArguments(
                argv=("echo",),
                max_output_bytes=100,
                max_return_bytes=101,
            )


class ShellRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "nested").mkdir()
        (self.root / "file.txt").write_text("not a directory")
        self.defaults = ShellDefaults(
            limits=ExecutionLimits(
                timeout_seconds=60,
                max_output_bytes=4096,
                max_return_bytes=2048,
                memory_bytes=256 * 1024 * 1024,
                cpu_seconds=30,
                max_processes=32,
                termination_grace_seconds=1,
            )
        )

    def request(self, **overrides):
        values = {"argv": ("pytest", "-q"), **overrides}
        return build_shell_request(
            ShellArguments(**values),
            project_root=self.root,
            defaults=self.defaults,
        )

    def test_maps_every_model_field_to_the_execution_request(self):
        request = self.request(
            working_directory="nested",
            backend="container",
            filesystem_mode="workspace-write",
            network_access=True,
            timeout_seconds=20,
            max_output_bytes=1024,
            max_return_bytes=512,
            memory_bytes=128 * 1024 * 1024,
            cpu_seconds=10,
            max_processes=8,
        )

        self.assertEqual(request.argv, ("pytest", "-q"))
        self.assertEqual(request.working_directory, self.root / "nested")
        self.assertEqual(request.backend, "container")
        self.assertEqual(request.filesystem_mode, "workspace-write")
        self.assertTrue(request.network_access)
        self.assertEqual(request.limits.timeout_seconds, 20)
        self.assertEqual(request.limits.max_output_bytes, 1024)
        self.assertEqual(request.limits.max_return_bytes, 512)
        self.assertEqual(request.limits.memory_bytes, 128 * 1024 * 1024)
        self.assertEqual(request.limits.cpu_seconds, 10)
        self.assertEqual(request.limits.max_processes, 8)
        self.assertEqual(request.limits.termination_grace_seconds, 1)

    def test_requested_limits_can_only_tighten_defaults(self):
        request = self.request(
            timeout_seconds=600,
            max_output_bytes=8192,
            max_return_bytes=8192,
            memory_bytes=1024 * 1024 * 1024,
            cpu_seconds=90,
            max_processes=256,
        )

        self.assertEqual(request.limits, self.defaults.limits)

    def test_nested_directory_resolves_to_a_canonical_workspace_path(self):
        request = self.request(working_directory="nested/..")

        self.assertEqual(request.working_directory, self.root)

    def test_rejects_absolute_missing_file_and_escape_paths(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(outside.rmdir)
        link = self.root / "escape"
        link.symlink_to(outside, target_is_directory=True)
        cases = (
            (str(self.root), "outside_workspace"),
            ("missing", "directory_not_found"),
            ("file.txt", "not_a_directory"),
            ("escape", "outside_workspace"),
            ("../outside", "outside_workspace"),
        )

        for requested, code in cases:
            with self.subTest(requested=requested):
                with self.assertRaises(ToolExecutionError) as caught:
                    self.request(working_directory=requested)
                self.assertEqual(caught.exception.code, code)


class ShellResultTests(unittest.TestCase):
    def result(self, status: str) -> ExecutionResult:
        backend = None if status == "denied" else "posix"
        exit_code = 7 if status == "failed" else 0 if status == "completed" else None
        reason = (
            "timeout"
            if status == "timed_out"
            else "cancellation"
            if status == "cancelled"
            else "memory_limit"
            if status == "limit_exceeded"
            else None
        )
        return ExecutionResult(
            status=status,  # type: ignore[arg-type]
            exit_code=exit_code,
            stdout="out",
            stderr="err",
            duration_seconds=1.23456,
            stdout_bytes=10,
            stderr_bytes=5,
            stdout_truncated=True,
            stderr_truncated=False,
            termination_reason=reason,  # type: ignore[arg-type]
            backend=backend,
            audit_id=f"run-{status}",
        )

    def test_formats_every_terminal_status_without_losing_fields(self):
        for status in (
            "completed",
            "failed",
            "timed_out",
            "cancelled",
            "denied",
            "limit_exceeded",
            "failed_to_start",
        ):
            with self.subTest(status=status):
                output = format_shell_result(self.result(status))
                self.assertEqual(output["status"], status)
                self.assertEqual(output["duration_seconds"], 1.235)
                self.assertEqual(output["stdout"], "out")
                self.assertEqual(output["stderr"], "err")
                self.assertEqual(output["stdout_bytes"], 10)
                self.assertTrue(output["stdout_truncated"])
                self.assertEqual(output["audit_id"], f"run-{status}")


if __name__ == "__main__":
    unittest.main()
