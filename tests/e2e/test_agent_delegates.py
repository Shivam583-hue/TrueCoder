"""A delegated subtask must do real work and report back without leaking context."""

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
from truecoder.agent.agent import subagent_registry
from truecoder.agent.events import AgentEventType
from truecoder.tools import ToolRegistry
from truecoder.tools.builtin import DelegateTool, ReadFileTool
from truecoder.tools.mutation_audit import MutationAudit

BUDGET = 64000


class DelegationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)
        (self.root / "parser.py").write_bytes(b"def parse(raw):\n    return raw\n")
        self.audit = MutationAudit(self.root / "audit.sqlite3")

    def _agent(self, parent: ScriptedModel, child: ScriptedModel) -> Agent:
        registry = ToolRegistry()
        registry.register(ReadFileTool(self.root))
        agent = Agent(
            llm_client=parent,
            tool_registry=registry,
            project_root=self.root,
            context_builder=ContextBuilder(
                system_prompt="parent system",
                max_input_tokens=BUDGET,
                token_counter=TokenCounter(),
            ),
            mutation_audit=self.audit,
        )

        async def approve(request):
            del request
            return ApprovalResponse.approve(ApprovalScope.ONCE)

        agent.approval_handler = approve

        async def run_child(task: str, max_iterations: int) -> object:
            sub = Agent(
                llm_client=child,
                tool_registry=subagent_registry(self.root, self.audit),
                project_root=self.root,
                context_builder=ContextBuilder(
                    system_prompt="child system",
                    max_input_tokens=BUDGET,
                    token_counter=TokenCounter(),
                ),
                max_iterations=max_iterations,
            )
            sub.approval_handler = approve
            from truecoder.tools.builtin.delegate import SubagentOutcome

            reply = ""
            calls_made = 0
            error = None
            try:
                async for event in sub.run(task):
                    if event.type is AgentEventType.TOOL_CALL:
                        calls_made += 1
                    elif event.type is AgentEventType.AGENT_ERROR:
                        error = str(event.data.get("error"))
                    elif event.type is AgentEventType.AGENT_END:
                        reply = str(event.data.get("response") or "")
            finally:
                await sub.close()
            return SubagentOutcome(reply=reply, tool_calls=calls_made, error=error)

        registry.register(DelegateTool(run_child))
        self.addAsyncCleanup(agent.close)
        return agent

    async def _run(self, agent: Agent, prompt: str) -> str:
        final = ""
        async for event in agent.run(prompt):
            if event.type is AgentEventType.AGENT_END:
                final = str(event.data.get("response") or "")
        return final

    async def test_a_subtask_does_real_work_and_reports_back(self):
        parent = ScriptedModel(
            [
                calls(("delegate", {"task": "read parser.py and say what parse does"})),
                says("The subagent says parse returns its input."),
            ]
        )
        child = ScriptedModel(
            [
                calls(("read_file", {"path": "parser.py"})),
                says("parse returns raw unchanged"),
            ]
        )
        agent = self._agent(parent, child)

        reply = await self._run(agent, "what does parse do?")

        payload = parent.tool_results()[-1]["output"]
        self.assertEqual(payload["reply"], "parse returns raw unchanged")
        self.assertEqual(payload["tool_calls"], 1)
        self.assertEqual(reply, "The subagent says parse returns its input.")

    async def test_the_subagent_never_sees_the_parent_conversation(self):
        parent = ScriptedModel(
            [calls(("delegate", {"task": "say hello"})), says("done")]
        )
        child = ScriptedModel([says("hello")])
        agent = self._agent(parent, child)

        await self._run(agent, "a very distinctive parent prompt")

        seen = str(child.requests)
        self.assertNotIn("a very distinctive parent prompt", seen)
        self.assertNotIn("parent system", seen)

    async def test_only_the_reply_crosses_back_not_the_transcript(self):
        parent = ScriptedModel(
            [calls(("delegate", {"task": "read parser.py"})), says("ok")]
        )
        child = ScriptedModel(
            [
                calls(("read_file", {"path": "parser.py"})),
                says("it returns raw"),
            ]
        )
        agent = self._agent(parent, child)

        await self._run(agent, "delegate it")

        payload = parent.tool_results()[-1]["output"]
        self.assertEqual(payload["reply"], "it returns raw")
        self.assertNotIn("def parse", str(payload))

    async def test_a_failing_subagent_is_an_error_the_parent_can_read(self):
        parent = ScriptedModel(
            [
                calls(("delegate", {"task": "do the impossible"})),
                says("The subagent could not do it."),
            ]
        )
        child = ScriptedModel([])
        agent = self._agent(parent, child)

        reply = await self._run(agent, "delegate it")

        self.assertEqual(reply, "The subagent could not do it.")


if __name__ == "__main__":
    unittest.main()
