from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from truecoder.agent import Agent, AgentEventType, AgentMode, ContextBuilder
from truecoder.agent.agent import subagent_runner
from truecoder.agent.events import AgentEvent
from truecoder.agent.mode import mode_allows_tool, mode_auto_approves, mode_from_name
from truecoder.agent.prompts import DEFAULT_SYSTEM_PROMPT
from truecoder.client.response import EventType, StreamEvent, TextDelta
from truecoder.tools import (
    BaseTool,
    ToolApproval,
    ToolArguments,
    ToolCall,
    ToolRegistry,
)


class _Counter:
    def count_message(self, _message) -> int:
        return 1


class _Arguments(ToolArguments):
    value: str


class _ProbeTool(BaseTool[_Arguments]):
    description = "Record whether this test tool ran."
    arguments_type = _Arguments
    approval = ToolApproval.REQUIRED

    def __init__(self, name: str) -> None:
        self.name = name
        self.ran = False

    async def run(self, arguments: _Arguments, invocation=None) -> dict[str, str]:
        del invocation
        self.ran = True
        return {"value": arguments.value}


class _ScriptedClient:
    def __init__(self, batches: list[list[StreamEvent]]) -> None:
        self.batches = batches
        self.calls: list[dict] = []

    async def chat_completion(self, messages, stream=True, tools=None):
        index = len(self.calls)
        self.calls.append({"messages": messages, "stream": stream, "tools": tools})
        for event in self.batches[index]:
            yield event

    async def close(self) -> None:
        return None


def _tool_call(name: str) -> StreamEvent:
    return StreamEvent(
        EventType.MESSAGE_COMPLETE,
        tool_calls=(ToolCall("call_1", name, '{"value":"hello"}'),),
        finish_reason="tool_calls",
    )


def _final() -> StreamEvent:
    return StreamEvent(
        EventType.MESSAGE_COMPLETE,
        text_delta=TextDelta("Done"),
        finish_reason="stop",
    )


def _agent(mode: AgentMode, tool: _ProbeTool) -> tuple[Agent, _ScriptedClient]:
    registry = ToolRegistry()
    registry.register(tool)
    client = _ScriptedClient([[_tool_call(tool.name)], [_final()]])
    return (
        Agent(
            llm_client=client,
            context_builder=ContextBuilder("test", 100, _Counter()),
            tool_registry=registry,
            mode=mode,
        ),
        client,
    )


class ModeModelTests(unittest.TestCase):
    def test_modes_cycle_in_the_displayed_order(self):
        self.assertIs(AgentMode.BUILD.next(), AgentMode.PLAN)
        self.assertIs(AgentMode.PLAN.next(), AgentMode.FULL_ACCESS)
        self.assertIs(AgentMode.FULL_ACCESS.next(), AgentMode.BUILD)

    def test_every_mode_name_parses(self):
        for mode in AgentMode:
            self.assertIs(mode_from_name(mode.value), mode)

    def test_plan_has_an_explicit_tool_allowlist(self):
        self.assertTrue(mode_allows_tool(AgentMode.PLAN, "read_file"))
        self.assertTrue(mode_allows_tool(AgentMode.PLAN, "update_plan"))
        self.assertFalse(mode_allows_tool(AgentMode.PLAN, "write_file"))
        self.assertFalse(mode_allows_tool(AgentMode.PLAN, "mcp__server__read"))

    def test_plan_auto_approves_local_reads_but_not_the_network(self):
        self.assertTrue(mode_auto_approves(AgentMode.PLAN, "read_file"))
        self.assertFalse(mode_auto_approves(AgentMode.PLAN, "web_fetch"))

    def test_the_base_prompt_requests_commit_attribution(self):
        self.assertIn(
            "Co-authored-by: TrueCoder-agent <truecoder39@gmail.com>",
            DEFAULT_SYSTEM_PROMPT,
        )
        self.assertIn("explicitly asks you not", DEFAULT_SYSTEM_PROMPT)


class ModeEnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def test_delegated_agents_inherit_the_active_turn_mode(self):
        parent = Mock()
        parent.project_root = Path("/workspace")
        parent.mode = AgentMode.BUILD
        parent.active_mode = AgentMode.FULL_ACCESS
        parent.approval_handler = AsyncMock()

        child = Mock()

        async def child_run(_task):
            yield AgentEvent.agent_end("done", None, "stop")

        child.run = child_run
        child.close = AsyncMock()

        with (
            patch(
                "truecoder.agent.agent.collect_environment",
                return_value=Mock(),
            ),
            patch(
                "truecoder.agent.agent.describe_environment",
                return_value="environment",
            ),
            patch(
                "truecoder.agent.agent.ContextBuilder.from_environment",
                return_value=Mock(),
            ),
            patch(
                "truecoder.agent.agent.subagent_registry",
                return_value=Mock(),
            ),
            patch("truecoder.agent.agent.Agent", return_value=child) as build,
        ):
            outcome = await subagent_runner(parent, Mock())("task", 3)

        self.assertEqual(outcome.reply, "done")
        self.assertIs(
            build.call_args.kwargs["mode"],
            AgentMode.FULL_ACCESS,
        )
        self.assertIs(child.approval_handler, parent.approval_handler)
        child.close.assert_awaited_once_with()

    async def test_plan_hides_and_rejects_a_mutating_tool(self):
        tool = _ProbeTool("write_file")
        agent, client = _agent(AgentMode.PLAN, tool)

        events = [event async for event in agent.run("plan this")]

        self.assertEqual(client.calls[0]["tools"], None)
        self.assertFalse(tool.ran)
        result = next(
            event for event in events if event.type is AgentEventType.TOOL_RESULT
        )
        self.assertIn("mode_restricted", result.data["content"])

    async def test_plan_auto_approves_an_allowed_local_read(self):
        tool = _ProbeTool("read_file")
        agent, client = _agent(AgentMode.PLAN, tool)

        events = [event async for event in agent.run("inspect this")]

        names = [item["function"]["name"] for item in client.calls[0]["tools"]]
        self.assertEqual(names, ["read_file"])
        self.assertTrue(tool.ran)
        self.assertNotIn(
            AgentEventType.APPROVAL_REQUESTED,
            [event.type for event in events],
        )

    async def test_full_access_auto_approves_a_guarded_tool(self):
        tool = _ProbeTool("dangerous_probe")
        agent, _client = _agent(AgentMode.FULL_ACCESS, tool)

        events = [event async for event in agent.run("do it")]

        self.assertTrue(tool.ran)
        self.assertNotIn(
            AgentEventType.APPROVAL_REQUESTED,
            [event.type for event in events],
        )

    async def test_build_keeps_the_existing_approval_flow(self):
        tool = _ProbeTool("guarded_probe")
        agent, _client = _agent(AgentMode.BUILD, tool)
        requested = []

        async def reject(request):
            requested.append(request)
            from truecoder.agent import ApprovalResponse

            return ApprovalResponse.reject()

        agent.approval_handler = reject
        events = [event async for event in agent.run("do it")]

        self.assertFalse(tool.ran)
        self.assertEqual(len(requested), 1)
        self.assertIn(
            AgentEventType.APPROVAL_REQUESTED,
            [event.type for event in events],
        )

    async def test_each_request_names_the_turn_mode(self):
        tool = _ProbeTool("read_file")
        agent, client = _agent(AgentMode.PLAN, tool)

        _ = [event async for event in agent.run("inspect this")]

        system_text = "\n".join(
            message["content"]
            for message in client.calls[0]["messages"]
            if message["role"] == "system"
        )
        self.assertIn("Current mode: Plan", system_text)

    async def test_plan_skips_checkpoints_and_configured_hooks(self):
        tool = _ProbeTool("read_file")
        agent, _client = _agent(AgentMode.PLAN, tool)
        agent._capture_checkpoint = AsyncMock()
        agent._run_hooks = AsyncMock()

        _ = [event async for event in agent.run("inspect this")]

        agent._capture_checkpoint.assert_not_awaited()
        agent._run_hooks.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
