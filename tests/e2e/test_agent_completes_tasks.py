"""What a user asks for must actually happen, end to end, with real tools."""

from __future__ import annotations

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
from truecoder.tools.builtin import (
    GrepTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from truecoder.tools.mutation_audit import MutationAudit

BUDGET = 64000


class TaskCompletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)
        (self.root / "app.py").write_bytes(
            b"".join(b"line %d\n" % number for number in range(1, 301))
        )
        (self.root / "notes.md").write_bytes(b"# Notes\n\nnothing yet\n")

    def _agent(self, model: ScriptedModel, *, with_execution: bool = False) -> Agent:
        registry = ToolRegistry()
        audit = MutationAudit(self.root / "mutations.sqlite3")
        registry.register(ReadFileTool(self.root))
        registry.register(GrepTool(self.root))
        registry.register(ListDirTool(self.root))
        registry.register(WriteFileTool(self.root, audit))
        agent = Agent(
            llm_client=model,
            tool_registry=registry,
            project_root=self.root,
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=BUDGET,
                token_counter=TokenCounter(),
            ),
            mutation_audit=audit,
            execution_bootstrap_config=(
                load_execution_config() if with_execution else None
            ),
        )
        self.addAsyncCleanup(agent.close)

        async def approve(request):
            del request
            return ApprovalResponse.approve(ApprovalScope.ONCE)

        agent.approval_handler = approve
        return agent

    async def _run(self, agent: Agent, prompt: str) -> list:
        return [event async for event in agent.run(prompt)]

    def _final(self, events: list) -> str:
        for event in reversed(events):
            if event.type is AgentEventType.AGENT_END:
                return str(event.data.get("response") or "")
        return ""

    async def test_a_bare_read_returns_the_file_and_finishes_the_turn(self):
        model = ScriptedModel(
            [
                calls(("read_file", {"path": "app.py"})),
                says("The file has 300 lines."),
            ]
        )
        agent = self._agent(model)

        events = await self._run(agent, "read app.py")

        result = model.last_result_for("path", "app.py")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("line 1\n", result["content"])
        self.assertIn("line 300\n", result["content"])
        self.assertFalse(result["has_more"])
        self.assertEqual(self._final(events), "The file has 300 lines.")

    async def test_a_read_result_reaches_the_model_unshortened(self):
        model = ScriptedModel([calls(("read_file", {"path": "app.py"})), says("done")])
        agent = self._agent(model)

        await self._run(agent, "read app.py")

        envelope = model.envelope_for("path", "app.py")
        assert envelope is not None
        self.assertNotIn("truncated", envelope)
        self.assertNotIn("omitted_characters", envelope)
        self.assertEqual(envelope["status"], "success")

    async def test_a_write_actually_changes_bytes_on_disk(self):
        model = ScriptedModel(
            [
                calls(
                    (
                        "write_file",
                        {"path": "notes.md", "content": "# Notes\n\nrewritten\n"},
                    )
                ),
                says("Rewrote notes."),
            ]
        )
        agent = self._agent(model)

        await self._run(agent, "rewrite notes.md")

        self.assertEqual(
            (self.root / "notes.md").read_bytes(),
            b"# Notes\n\nrewritten\n",
        )

    async def test_a_rejected_write_leaves_the_file_alone(self):
        model = ScriptedModel(
            [
                calls(("write_file", {"path": "notes.md", "content": "destroyed"})),
                says("I did not change it."),
            ]
        )
        agent = self._agent(model)

        async def reject(request):
            del request
            return ApprovalResponse.reject()

        agent.approval_handler = reject

        await self._run(agent, "rewrite notes.md")

        self.assertEqual(
            (self.root / "notes.md").read_bytes(),
            b"# Notes\n\nnothing yet\n",
        )

    async def test_a_bad_argument_costs_a_call_and_not_the_turn(self):
        model = ScriptedModel(
            [
                calls(("read_file", {"path": "app.py", "start_line": 0})),
                says("Sorry, I got that wrong."),
            ]
        )
        agent = self._agent(model)

        events = await self._run(agent, "read app.py")

        results = model.tool_results()
        self.assertTrue(any(r.get("status") == "error" for r in results))
        self.assertEqual(self._final(events), "Sorry, I got that wrong.")

    async def test_a_bracketed_error_survives_verbatim(self):
        model = ScriptedModel(
            [
                calls(("grep", {"pattern": "[unclosed", "path": "."})),
                says("That pattern was invalid."),
            ]
        )
        agent = self._agent(model)

        events = await self._run(agent, "search")

        self.assertEqual(self._final(events), "That pattern was invalid.")

    async def test_several_tools_in_one_turn_all_report_back(self):
        model = ScriptedModel(
            [
                calls(
                    ("list_dir", {"path": "."}),
                    ("read_file", {"path": "notes.md"}),
                ),
                says("Looked at both."),
            ]
        )
        agent = self._agent(model)

        events = await self._run(agent, "look around")

        self.assertEqual(len(model.tool_results()), 2)
        self.assertEqual(self._final(events), "Looked at both.")


if __name__ == "__main__":
    unittest.main()
