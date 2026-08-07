from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from truecoder.execution.context import ExecutionContextFactory
from truecoder.execution.errors import ExecutionInfrastructureError
from truecoder.execution.models import ExecutionContext, ExecutionResult
from truecoder.hooks.models import Hook
from truecoder.hooks.runner import (
    HookRunner,
    build_hook_request,
    hook_limits,
    resolve_working_directory,
)


def _hook(**overrides) -> Hook:
    payload = {
        "name": "format",
        "event": "turn_end",
        "command": ("ruff", "format", "."),
    }
    payload.update(overrides)
    return Hook(**payload)  # type: ignore[arg-type]


def _result(
    status: str = "completed",
    exit_code: int | None = 0,
    termination_reason: str | None = None,
    reason_message: str | None = None,
    stderr: str = "",
) -> ExecutionResult:
    return ExecutionResult(
        status=status,  # type: ignore[arg-type]
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
        duration_seconds=0.1,
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        termination_reason=termination_reason,  # type: ignore[arg-type]
        backend="posix",
        audit_id="audit_1",
        reason_code=None,
        reason_message=reason_message,
    )


class RecordingService:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result or _result()
        self.error = error
        self.requests: list = []
        self.contexts: list[ExecutionContext] = []
        self.authorised_during: list[bool] = []
        self.watch: set[str] | None = None

    async def execute(self, request, context, *, cancellation_source=None):
        del cancellation_source
        self.requests.append(request)
        self.contexts.append(context)
        if self.watch is not None:
            self.authorised_during.append(context.tool_call_id in self.watch)
        if self.error is not None:
            raise self.error
        return self.result


class HookRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def test_a_hook_runs_the_exact_command(self):
        request = build_hook_request(_hook(), self.root)

        self.assertEqual(request.mode, "exec")
        self.assertEqual(request.argv, ("ruff", "format", "."))
        self.assertIsNone(request.script)

    def test_a_hook_runs_with_host_access_like_a_git_hook(self):
        request = build_hook_request(_hook(), self.root)

        self.assertEqual(request.filesystem_mode, "host")
        self.assertTrue(request.network_access)

    def test_a_hook_runs_locally_where_the_toolchain_is(self):
        self.assertEqual(build_hook_request(_hook(), self.root).backend, "local")

    def test_the_hook_timeout_reaches_the_limits(self):
        limits = hook_limits(_hook(timeout_seconds=12.5))

        self.assertEqual(limits.timeout_seconds, 12.5)

    def test_hook_output_is_bounded_more_tightly_than_shell(self):
        limits = hook_limits(_hook())

        self.assertLessEqual(limits.max_output_bytes, 1024 * 1024)
        self.assertLessEqual(limits.max_return_bytes, 64 * 1024)

    def test_a_relative_working_directory_is_resolved(self):
        (self.root / "sub").mkdir()

        resolved = resolve_working_directory(self.root, "sub")

        self.assertEqual(resolved, (self.root / "sub").resolve())

    def test_the_workspace_root_is_allowed(self):
        self.assertEqual(resolve_working_directory(self.root, "."), self.root)

    def test_an_absolute_working_directory_is_refused(self):
        with self.assertRaises(ValueError):
            resolve_working_directory(self.root, "/etc")

    def test_an_escaping_working_directory_is_refused(self):
        with self.assertRaises(ValueError):
            resolve_working_directory(self.root, "../outside")


class HookRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.factory = ExecutionContextFactory()
        self.addCleanup(self._directory.cleanup)

    def _runner(self, service, pre_authorise=None) -> HookRunner:
        return HookRunner(
            service,
            self.root,
            context_factory=self.factory,
            pre_authorise=pre_authorise,
        )

    async def _run(self, service, hooks=None, pre_authorise=None):
        return await self._runner(service, pre_authorise).run(
            hooks or [_hook()],
            session_id="session_1",
            turn_id="turn_1",
        )

    async def test_a_successful_hook_reports_ok(self):
        outcomes = await self._run(RecordingService())

        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].ok)
        self.assertIn("ok", outcomes[0].summary)

    async def test_a_failing_hook_reports_its_exit_code(self):
        outcomes = await self._run(RecordingService(_result(status="failed", exit_code=1)))

        self.assertFalse(outcomes[0].ok)
        self.assertEqual(outcomes[0].exit_code, 1)
        self.assertIn("exited 1", outcomes[0].summary)

    async def test_a_timed_out_hook_reports_its_status(self):
        outcomes = await self._run(
            RecordingService(
                _result(
                    status="timed_out",
                    exit_code=None,
                    termination_reason="timeout",
                )
            )
        )

        self.assertFalse(outcomes[0].ok)
        self.assertEqual(outcomes[0].status, "timed_out")

    async def test_infrastructure_failure_never_raises(self):
        service = RecordingService(
            error=ExecutionInfrastructureError("broken", operation="execute")
        )

        outcomes = await self._run(service)

        self.assertEqual(outcomes[0].status, "infrastructure_error")

    async def test_an_unexpected_failure_never_raises(self):
        outcomes = await self._run(RecordingService(error=RuntimeError("boom")))

        self.assertEqual(outcomes[0].status, "failed")
        self.assertIn("boom", outcomes[0].detail)

    async def test_an_escaping_hook_is_refused_before_running(self):
        service = RecordingService()

        outcomes = await self._run(
            service,
            hooks=[_hook(working_directory="../outside")],
        )

        self.assertEqual(outcomes[0].status, "refused")
        self.assertEqual(service.requests, [])

    async def test_every_hook_runs_in_order(self):
        service = RecordingService()
        hooks = [_hook(name="one"), _hook(name="two")]

        outcomes = await self._run(service, hooks=hooks)

        self.assertEqual([o.hook.name for o in outcomes], ["one", "two"])
        self.assertEqual(len(service.requests), 2)

    async def test_a_hook_carries_the_session_and_turn(self):
        service = RecordingService()

        await self._run(service)

        context = service.contexts[0]
        self.assertEqual(context.session_id, "session_1")
        self.assertEqual(context.turn_id, "turn_1")
        self.assertTrue(context.tool_call_id.startswith("hook_"))

    async def test_a_hook_is_pre_authorised_only_while_it_runs(self):
        service = RecordingService()
        authorised: set[str] = set()
        service.watch = authorised

        def pre_authorise(call_id: str):
            authorised.add(call_id)
            return lambda: authorised.discard(call_id)

        await self._run(service, pre_authorise=pre_authorise)

        self.assertEqual(service.authorised_during, [True])
        self.assertEqual(authorised, set())

    async def test_a_refusal_reason_reaches_the_outcome(self):
        service = RecordingService(
            _result(
                status="failed_to_start",
                exit_code=None,
                reason_message="the executable was not found",
            )
        )

        outcomes = await self._run(service)

        self.assertIn("was not found", outcomes[0].detail)

    async def test_stderr_is_used_when_there_is_no_reason(self):
        service = RecordingService(
            _result(status="failed", exit_code=2, stderr="syntax error\n")
        )

        outcomes = await self._run(service)

        self.assertEqual(outcomes[0].detail, "syntax error")

    async def test_pre_authorisation_is_released_after_a_failure(self):
        service = RecordingService(error=RuntimeError("boom"))
        authorised: set[str] = set()

        def pre_authorise(call_id: str):
            authorised.add(call_id)
            return lambda: authorised.discard(call_id)

        await self._run(service, pre_authorise=pre_authorise)

        self.assertEqual(authorised, set())


if __name__ == "__main__":
    unittest.main()
