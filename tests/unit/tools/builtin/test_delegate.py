"""A subtask runs in its own context and comes back as a report, not a command."""

from __future__ import annotations

import json
import unittest

from truecoder.tools.base import ToolApproval, ToolArgumentError, ToolExecutionError
from truecoder.tools.builtin.delegate import (
    MAX_DELEGATE_DEPTH,
    MAX_REPLY_CHARACTERS,
    MAX_TASK_CHARACTERS,
    DelegateTool,
    SubagentOutcome,
)


def _runner(outcome: SubagentOutcome | None = None, error: Exception | None = None):
    seen: list[tuple[str, int]] = []

    async def run(task: str, max_iterations: int) -> SubagentOutcome:
        seen.append((task, max_iterations))
        if error is not None:
            raise error
        return outcome or SubagentOutcome(reply="done", tool_calls=2)

    run.seen = seen  # type: ignore[attr-defined]
    return run


class DelegateContractTests(unittest.TestCase):
    def test_approval_is_required(self):
        self.assertIs(DelegateTool(_runner()).approval, ToolApproval.REQUIRED)

    def test_the_task_is_the_only_required_argument(self):
        parameters = DelegateTool(_runner()).definition().parameters

        self.assertEqual(parameters["required"], ["task"])
        self.assertIn("max_iterations", parameters["properties"])

    def test_the_description_says_the_subagent_starts_fresh(self):
        description = DelegateTool(_runner()).description.lower()

        self.assertIn("empty conversation", description)
        self.assertIn("report", description)

    def test_an_empty_task_is_refused(self):
        with self.assertRaises(ToolArgumentError):
            DelegateTool(_runner()).parse_arguments(json.dumps({"task": ""}))

    def test_an_oversized_task_is_refused(self):
        with self.assertRaises(ToolArgumentError):
            DelegateTool(_runner()).parse_arguments(
                json.dumps({"task": "x" * (MAX_TASK_CHARACTERS + 1)})
            )

    def test_an_absurd_iteration_budget_is_refused(self):
        with self.assertRaises(ToolArgumentError):
            DelegateTool(_runner()).parse_arguments(
                json.dumps({"task": "go", "max_iterations": 500})
            )

    def test_a_non_callable_runner_is_rejected(self):
        with self.assertRaises(TypeError):
            DelegateTool("not callable")  # type: ignore[arg-type]

    def test_a_negative_depth_is_rejected(self):
        with self.assertRaises(ValueError):
            DelegateTool(_runner(), depth=-1)


class DelegateRunTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, tool: DelegateTool, **arguments):
        payload = {"task": "find the parser", **arguments}
        return await tool.run(tool.parse_arguments(json.dumps(payload)))

    async def test_the_reply_comes_back_to_the_caller(self):
        tool = DelegateTool(_runner(SubagentOutcome(reply="it is in parser.py")))

        result = await self._run(tool)

        self.assertEqual(result["reply"], "it is in parser.py")
        self.assertEqual(result["task"], "find the parser")
        self.assertFalse(result["truncated"])

    async def test_the_subagent_tool_calls_are_reported(self):
        tool = DelegateTool(_runner(SubagentOutcome(reply="x", tool_calls=7)))

        result = await self._run(tool)

        self.assertEqual(result["tool_calls"], 7)

    async def test_the_iteration_budget_reaches_the_subagent(self):
        runner = _runner()
        tool = DelegateTool(runner)

        await self._run(tool, max_iterations=3)

        self.assertEqual(runner.seen[0][1], 3)  # type: ignore[attr-defined]

    async def test_a_subagent_failure_is_a_domain_error(self):
        tool = DelegateTool(_runner(SubagentOutcome(error="it gave up")))

        with self.assertRaises(ToolExecutionError) as caught:
            await self._run(tool)

        self.assertEqual(caught.exception.code, "subagent_failed")
        self.assertIn("it gave up", caught.exception.message)

    async def test_a_runner_that_raises_is_a_domain_error(self):
        tool = DelegateTool(_runner(error=RuntimeError("no model")))

        with self.assertRaises(ToolExecutionError) as caught:
            await self._run(tool)

        self.assertEqual(caught.exception.code, "subagent_unavailable")

    async def test_an_enormous_reply_is_bounded(self):
        tool = DelegateTool(
            _runner(SubagentOutcome(reply="y" * (MAX_REPLY_CHARACTERS * 2)))
        )

        result = await self._run(tool)

        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["reply"]), MAX_REPLY_CHARACTERS)

    async def test_a_subagent_cannot_delegate_again(self):
        tool = DelegateTool(_runner(), depth=MAX_DELEGATE_DEPTH)

        with self.assertRaises(ToolExecutionError) as caught:
            await self._run(tool)

        self.assertEqual(caught.exception.code, "delegation_too_deep")

    async def test_a_refused_delegation_never_starts_a_subagent(self):
        runner = _runner()
        tool = DelegateTool(runner, depth=MAX_DELEGATE_DEPTH)

        with self.assertRaises(ToolExecutionError):
            await self._run(tool)

        self.assertEqual(runner.seen, [])  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
