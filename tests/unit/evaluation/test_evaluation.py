"""Scoring the agent on fixed tasks, so better stops being a feeling."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers.turns import ScriptedModel, TokenCounter, calls, says
from truecoder.agent import Agent, ApprovalResponse, ApprovalScope, ContextBuilder
from truecoder.evaluation import (
    DEFAULT_TASKS,
    EvalReport,
    EvalResult,
    EvalTask,
    all_of,
    file_contains,
    file_unchanged,
    materialise,
    run_suite,
    run_task,
)
from truecoder.tools import ToolRegistry
from truecoder.tools.builtin import ReadFileTool, WriteFileTool
from truecoder.tools.mutation_audit import MutationAudit


def _agent_factory(batches):
    def build(root: Path, task: EvalTask):
        registry = ToolRegistry()
        audit = MutationAudit(root / "audit.sqlite3")
        registry.register(ReadFileTool(root))
        registry.register(WriteFileTool(root, audit))
        agent = Agent(
            llm_client=ScriptedModel(batches),
            tool_registry=registry,
            project_root=root,
            context_builder=ContextBuilder(
                system_prompt="test",
                max_input_tokens=64000,
                token_counter=TokenCounter(),
            ),
            mutation_audit=audit,
            max_iterations=task.max_iterations,
        )

        async def approve(request):
            del request
            return ApprovalResponse.approve(ApprovalScope.ONCE)

        agent.approval_handler = approve
        return agent

    return build


class CheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def test_file_contains_passes_and_fails(self):
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")

        self.assertIsNone(file_contains("a.py", "x = 1")(self.root))
        self.assertIsNotNone(file_contains("a.py", "y")(self.root))

    def test_a_missing_file_is_reported(self):
        self.assertIn("does not exist", str(file_contains("gone.py", "x")(self.root)))

    def test_file_unchanged_detects_a_modification(self):
        (self.root / "a.py").write_text("changed\n", encoding="utf-8")

        self.assertIsNotNone(file_unchanged("a.py", "original\n")(self.root))

    def test_all_of_reports_the_first_failure(self):
        (self.root / "a.py").write_text("x\n", encoding="utf-8")

        failure = all_of(file_contains("a.py", "x"), file_contains("a.py", "z"))(
            self.root
        )

        self.assertIn("z", str(failure))


class MaterialiseTests(unittest.TestCase):
    def test_nested_files_are_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = EvalTask(
                name="t",
                prompt="p",
                files={"src/pkg/mod.py": "x = 1\n"},
            )

            materialise(task, root)

            self.assertEqual(
                (root / "src/pkg/mod.py").read_text(encoding="utf-8"),
                "x = 1\n",
            )


class RunTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_task_the_agent_completes_passes(self):
        task = EvalTask(
            name="write-version",
            prompt="create VERSION",
            check=file_contains("VERSION", "1.0.0"),
        )
        build = _agent_factory(
            [
                calls(("write_file", {"path": "VERSION", "content": "1.0.0\n"})),
                says("done"),
            ]
        )

        result = await run_task(task, build)

        self.assertTrue(result.passed)
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(result.reply, "done")

    async def test_a_task_the_agent_ignores_fails_with_a_reason(self):
        task = EvalTask(
            name="write-version",
            prompt="create VERSION",
            check=file_contains("VERSION", "1.0.0"),
        )

        result = await run_task(task, _agent_factory([says("I did nothing")]))

        self.assertFalse(result.passed)
        self.assertIn("VERSION", str(result.detail))

    async def test_a_task_without_a_check_passes_when_the_turn_finishes(self):
        task = EvalTask(name="just-talk", prompt="say hi")

        result = await run_task(task, _agent_factory([says("hi")]))

        self.assertTrue(result.passed)

    async def test_a_crashing_agent_fails_the_task_rather_than_the_suite(self):
        def build(root, task):
            del root, task
            raise RuntimeError("could not build")

        result = await run_task(EvalTask(name="t", prompt="p"), build)

        self.assertFalse(result.passed)
        self.assertIn("could not build", str(result.detail))

    async def test_the_workspace_is_seeded_before_the_agent_runs(self):
        task = EvalTask(
            name="read-it",
            prompt="read it",
            files={"seed.txt": "hello\n"},
            check=file_contains("seed.txt", "hello"),
        )

        result = await run_task(task, _agent_factory([says("read")]))

        self.assertTrue(result.passed)

    async def test_a_non_task_is_rejected(self):
        with self.assertRaises(TypeError):
            await run_task(object(), _agent_factory([says("x")]))  # type: ignore[arg-type]


class ReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_suite_scores_every_task(self):
        tasks = (
            EvalTask(name="a", prompt="p"),
            EvalTask(name="b", prompt="p"),
        )

        report = await run_suite(tasks, _agent_factory([says("ok")]))

        self.assertEqual(report.total, 2)
        self.assertEqual(report.passed, 2)
        self.assertTrue(report.is_clean)
        self.assertIn("2/2", report.summary)

    def test_a_mixed_report_is_not_clean(self):
        report = EvalReport(
            results=(
                EvalResult(task="a", passed=True),
                EvalResult(task="b", passed=False, detail="nope"),
            )
        )

        self.assertFalse(report.is_clean)
        self.assertIn("1/2", report.summary)
        self.assertIn("FAIL", report.results[1].summary)

    def test_an_empty_report_is_never_clean(self):
        self.assertFalse(EvalReport().is_clean)


class ShippedTaskTests(unittest.TestCase):
    def test_the_default_tasks_are_well_formed(self):
        self.assertGreaterEqual(len(DEFAULT_TASKS), 4)
        names = [task.name for task in DEFAULT_TASKS]
        self.assertEqual(len(names), len(set(names)))
        for task in DEFAULT_TASKS:
            with self.subTest(task=task.name):
                self.assertTrue(task.prompt.strip())
                self.assertIsNotNone(task.check)

    def test_a_task_needs_a_prompt(self):
        with self.assertRaises(ValueError):
            EvalTask(name="t", prompt="  ")

    def test_a_task_needs_a_name(self):
        with self.assertRaises(ValueError):
            EvalTask(name="", prompt="p")


if __name__ == "__main__":
    unittest.main()
