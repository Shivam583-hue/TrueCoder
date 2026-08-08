"""A command the model asks for must run somewhere the project actually works."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers.turns import ScriptedModel, TokenCounter, calls, says
from truecoder.agent import (
    Agent,
    ApprovalResponse,
    ApprovalScope,
    ContextBuilder,
)
from truecoder.agent.events import AgentEventType
from truecoder.execution.configuration import load_execution_config
from truecoder.tools import ToolRegistry
from truecoder.tools.builtin import ReadFileTool

BUDGET = 64000
LOCAL_BACKENDS = frozenset({"posix", "windows"})


class CommandExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)
        (self.root / "marker.txt").write_bytes(b"present\n")

    async def _agent(self, model: ScriptedModel) -> Agent:
        registry = ToolRegistry()
        registry.register(ReadFileTool(self.root))
        agent = Agent(
            llm_client=model,
            tool_registry=registry,
            project_root=self.root,
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=BUDGET,
                token_counter=TokenCounter(),
            ),
            execution_bootstrap_config=load_execution_config(),
        )

        async def approve(request):
            del request
            return ApprovalResponse.approve(ApprovalScope.ONCE)

        agent.approval_handler = approve
        self.addAsyncCleanup(agent.close)
        runtime = await agent.initialize_execution()
        if runtime is None or not runtime.shell_available:
            self.skipTest("shell execution is unavailable on this host")
        return agent

    async def _run(self, agent: Agent, prompt: str) -> list:
        return [event async for event in agent.run(prompt)]

    def _final(self, events: list) -> str:
        for event in reversed(events):
            if event.type is AgentEventType.AGENT_END:
                return str(event.data.get("response") or "")
        return ""

    def _shell_result(self, model: ScriptedModel) -> dict:
        for payload in reversed(model.tool_results()):
            output = payload.get("output")
            if isinstance(output, dict) and "exit_code" in output:
                return output
        raise AssertionError("no shell result reached the model")

    async def test_a_default_command_runs_on_this_machine(self):
        model = ScriptedModel(
            [
                calls(("shell", {"mode": "exec", "argv": [sys.executable, "-V"]})),
                says("Checked the interpreter."),
            ]
        )
        agent = await self._agent(model)

        events = await self._run(agent, "check python")

        result = self._shell_result(model)
        self.assertIn(result["backend"], LOCAL_BACKENDS)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(self._final(events), "Checked the interpreter.")

    async def test_the_command_sees_the_real_workspace(self):
        model = ScriptedModel(
            [
                calls(
                    (
                        "shell",
                        {
                            "mode": "exec",
                            "argv": [
                                sys.executable,
                                "-c",
                                "print(open('marker.txt').read().strip())",
                            ],
                        },
                    )
                ),
                says("Read the marker."),
            ]
        )
        agent = await self._agent(model)

        await self._run(agent, "read the marker")

        result = self._shell_result(model)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("present", result["stdout"])

    async def test_the_project_interpreter_can_import_its_own_package(self):
        model = ScriptedModel(
            [
                calls(
                    (
                        "shell",
                        {
                            "mode": "exec",
                            "argv": [sys.executable, "-c", "import truecoder"],
                        },
                    )
                ),
                says("The package imports."),
            ]
        )
        agent = await self._agent(model)

        await self._run(agent, "import the package")

        self.assertEqual(self._shell_result(model)["exit_code"], 0)

    async def test_a_failing_command_is_data_and_not_a_broken_turn(self):
        model = ScriptedModel(
            [
                calls(
                    (
                        "shell",
                        {
                            "mode": "exec",
                            "argv": [sys.executable, "-c", "raise SystemExit(3)"],
                        },
                    )
                ),
                says("It exited with 3."),
            ]
        )
        agent = await self._agent(model)

        events = await self._run(agent, "run it")

        result = self._shell_result(model)
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self._final(events), "It exited with 3.")

    async def test_large_output_is_never_silently_lost(self):
        model = ScriptedModel(
            [
                calls(
                    (
                        "shell",
                        {
                            "mode": "exec",
                            "argv": [sys.executable, "-c", "print('x' * 200000)"],
                        },
                    )
                ),
                says("That was a lot of output."),
            ]
        )
        agent = await self._agent(model)

        events = await self._run(agent, "make noise")

        payload = model.tool_results()[-1]
        if payload.get("truncated"):
            self.assertGreater(payload["omitted_characters"], 0)
            self.assertIn("shortened", payload["note"])
            self.assertTrue(payload["output"])
        else:
            self.assertEqual(payload["output"]["exit_code"], 0)
        self.assertEqual(self._final(events), "That was a lot of output.")


if __name__ == "__main__":
    unittest.main()
