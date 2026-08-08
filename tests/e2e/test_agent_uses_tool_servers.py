"""A configured tool server must reach the model and stay untrusted on the way back."""

from __future__ import annotations

import json
import os
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
from truecoder.mcp.client import McpClient
from truecoder.mcp.configuration import McpServer, McpSuite
from truecoder.mcp.manager import McpManager
from truecoder.mcp.tool import UNTRUSTED_NOTE
from truecoder.tools import ToolRegistry
from truecoder.tools.builtin import ReadFileTool

BUDGET = 64000
SERVER = Path(__file__).resolve().parents[1] / "helpers" / "mcp_server.py"


def _factory(mode: str | None = None):
    def build(server: McpServer, root: Path) -> McpClient:
        environment = os.environ.copy()
        if mode is not None:
            environment["FAKE_MCP_MODE"] = mode
        return McpClient(
            list(server.command),
            cwd=root,
            env=environment,
            request_timeout=15.0,
        )

    return build


class ToolServerTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    async def _agent(
        self,
        model: ScriptedModel,
        *,
        suite: McpSuite | None = None,
        mode: str | None = None,
    ) -> Agent:
        registry = ToolRegistry()
        registry.register(ReadFileTool(self.root))
        servers = suite or McpSuite(
            servers=(McpServer(name="files", command=(sys.executable, str(SERVER))),)
        )
        manager = McpManager(servers, self.root, client_factory=_factory(mode))
        agent = Agent(
            llm_client=model,
            tool_registry=registry,
            project_root=self.root,
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=BUDGET,
                token_counter=TokenCounter(),
            ),
            mcp_manager=manager,
        )

        async def approve(request):
            del request
            return ApprovalResponse.approve(ApprovalScope.ONCE)

        agent.approval_handler = approve
        self.addAsyncCleanup(agent.close)
        await agent.initialize_mcp()
        return agent

    async def _run(self, agent: Agent, prompt: str) -> list:
        return [event async for event in agent.run(prompt)]

    def _final(self, events: list) -> str:
        for event in reversed(events):
            if event.type is AgentEventType.AGENT_END:
                return str(event.data.get("response") or "")
        return ""

    async def test_server_tools_are_offered_to_the_model(self):
        agent = await self._agent(ScriptedModel([]))

        names = [tool.name for tool in agent.tool_registry.all()]

        self.assertIn("mcp__files__echo", names)
        self.assertIn("mcp__files__add", names)
        self.assertIn("read_file", names)

    async def test_the_model_can_call_a_server_tool_and_finish_the_turn(self):
        model = ScriptedModel(
            [
                calls(("mcp__files__echo", {"text": "hello from the server"})),
                says("The server said hello."),
            ]
        )
        agent = await self._agent(model)

        events = await self._run(agent, "ask the server")

        payload = model.tool_results()[-1]
        self.assertEqual(payload["output"]["content"], "hello from the server")
        self.assertEqual(payload["output"]["server"], "files")
        self.assertEqual(self._final(events), "The server said hello.")

    async def test_the_result_is_labelled_untrusted_for_the_model(self):
        model = ScriptedModel(
            [calls(("mcp__files__echo", {"text": "hi"})), says("done")]
        )
        agent = await self._agent(model)

        await self._run(agent, "ask the server")

        self.assertEqual(model.tool_results()[-1]["output"]["note"], UNTRUSTED_NOTE)

    async def test_the_prompt_warns_about_third_party_tools(self):
        agent = await self._agent(ScriptedModel([]))

        prompt = agent.context_builder.system_prompt

        self.assertIn("mcp__", prompt)
        self.assertIn("untrusted", prompt.lower())

    async def test_a_rejected_server_call_never_reaches_the_server(self):
        model = ScriptedModel(
            [
                calls(("mcp__files__echo", {"text": "hi"})),
                says("I did not call it."),
            ]
        )
        agent = await self._agent(model)

        async def reject(request):
            del request
            return ApprovalResponse.reject()

        agent.approval_handler = reject

        events = await self._run(agent, "ask the server")

        self.assertEqual(self._final(events), "I did not call it.")

    async def test_a_bad_argument_costs_a_call_and_not_the_turn(self):
        model = ScriptedModel(
            [
                calls(("mcp__files__echo", {})),
                says("Sorry, I got that wrong."),
            ]
        )
        agent = await self._agent(model)

        events = await self._run(agent, "ask the server")

        results = model.tool_results()
        self.assertTrue(any(r.get("status") == "error" for r in results))
        self.assertEqual(self._final(events), "Sorry, I got that wrong.")

    async def test_a_broken_server_leaves_the_builtin_tools_working(self):
        (self.root / "notes.md").write_bytes(b"still here\n")
        suite = McpSuite(
            servers=(McpServer(name="broken", command=("truecoder-no-such-server",)),)
        )
        model = ScriptedModel(
            [calls(("read_file", {"path": "notes.md"})), says("Read it anyway.")]
        )
        agent = await self._agent(model, suite=suite)

        events = await self._run(agent, "read notes")

        self.assertEqual(self._final(events), "Read it anyway.")
        self.assertIn("still here", json.dumps(model.tool_results()))

    async def test_a_tool_reporting_an_error_is_data_the_model_can_read(self):
        model = ScriptedModel(
            [
                calls(("mcp__files__echo", {"text": "hi"})),
                says("The server refused."),
            ]
        )
        agent = await self._agent(model, mode="tool_error")

        events = await self._run(agent, "ask the server")

        self.assertEqual(model.tool_results()[-1]["output"]["status"], "error")
        self.assertEqual(self._final(events), "The server refused.")


if __name__ == "__main__":
    unittest.main()
