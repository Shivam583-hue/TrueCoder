from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from tests.helpers.platforms import requires_symlinks
from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.context import ExecutionContextFactory
from truecoder.execution.errors import AuditUnavailableError
from truecoder.execution.models import ExecutionLimits, ExecutionResult
from truecoder.tools import (
    ToolCall,
    ToolExecutor,
    ToolInvocationContext,
    ToolRegistry,
    ToolResultStatus,
)
from truecoder.tools.base import ToolApproval, ToolExecutionError
from truecoder.tools.builtin.shell import (
    ShellArguments,
    ShellDefaults,
    ShellTool,
    build_shell_request,
    format_shell_result,
)


class RecordingService:
    def __init__(
        self,
        result: ExecutionResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls = []

    async def execute(
        self,
        request,
        context,
        *,
        cancellation_source,
    ):
        self.calls.append((request, context, cancellation_source))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def execution_result(status: str = "completed") -> ExecutionResult:
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

    def test_defaults_reach_this_machine_rather_than_the_container(self):
        request = self.request()

        self.assertEqual(request.filesystem_mode, "host")
        self.assertTrue(request.network_access)
        self.assertEqual(request.backend, "auto")

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

    @requires_symlinks
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
                output = format_shell_result(execution_result(status))
                self.assertEqual(output["status"], status)
                self.assertEqual(output["duration_seconds"], 1.235)
                self.assertEqual(output["stdout"], "out")
                self.assertEqual(output["stderr"], "err")
                self.assertEqual(output["stdout_bytes"], 10)
                self.assertTrue(output["stdout_truncated"])
                self.assertEqual(output["audit_id"], f"run-{status}")


class ShellToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.invocation = ToolInvocationContext(
            execution=ExecutionContextFactory(
                execution_id_factory=lambda: "exec-shell",
                clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
            ).create(
                tool_call_id="call-shell",
                session_id="session-shell",
                turn_id="turn-shell",
                project_root=self.root,
            ),
            cancellation_source=CancellationSource(),
        )

    async def test_calls_the_service_once_with_the_exact_invocation(self):
        service = RecordingService(execution_result())
        tool = ShellTool(self.root, service)

        output = await tool.run(
            ShellArguments(argv=("python", "-V")),
            self.invocation,
        )

        self.assertEqual(output["status"], "completed")
        self.assertEqual(len(service.calls), 1)
        request, context, source = service.calls[0]
        self.assertEqual(request.argv, ("python", "-V"))
        self.assertIs(context, self.invocation.execution)
        self.assertIs(source, self.invocation.cancellation_source)
        self.assertIs(tool.approval, ToolApproval.NOT_REQUIRED)

    async def test_nonzero_exit_is_successful_tool_data(self):
        service = RecordingService(execution_result("failed"))
        registry = ToolRegistry()
        registry.register(ShellTool(self.root, service))

        result = await ToolExecutor(registry).execute(
            ToolCall(
                "call-shell",
                "shell",
                '{"argv":["python","-c","raise SystemExit(7)"]}',
            ),
            invocation=self.invocation,
        )

        self.assertEqual(result.status, ToolResultStatus.SUCCESS)
        self.assertEqual(result.output["status"], "failed")
        self.assertEqual(result.output["exit_code"], 7)

    async def test_infrastructure_failure_is_sanitized(self):
        service = RecordingService(
            error=AuditUnavailableError(
                "private audit database path and native error",
                execution_id="exec-shell",
                operation="admit",
            )
        )
        tool = ShellTool(self.root, service)

        with self.assertRaises(ToolExecutionError) as caught:
            await tool.run(
                ShellArguments(argv=("python", "-V")),
                self.invocation,
            )

        self.assertEqual(caught.exception.code, "shell_infrastructure_error")
        self.assertNotIn("private", caught.exception.message)

    async def test_missing_invocation_never_calls_the_service(self):
        service = RecordingService(execution_result())
        tool = ShellTool(self.root, service)

        with self.assertRaises(ToolExecutionError) as caught:
            await tool.run(ShellArguments(argv=("python", "-V")))

        self.assertEqual(caught.exception.code, "missing_invocation_context")
        self.assertEqual(service.calls, [])


if __name__ == "__main__":
    unittest.main()
