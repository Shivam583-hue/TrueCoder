import unittest

from tests.unit.agent.test_agent import FixedTokenCounter
from truecoder.agent import Agent, ApprovalResponse, ApprovalScope, ContextBuilder
from truecoder.agent.events import AgentEventType
from truecoder.client.response import EventType, StreamEvent, TextDelta
from truecoder.tools import ToolApproval, ToolArguments, ToolCall, ToolRegistry
from truecoder.tools.base import BaseTool


class Args(ToolArguments):
    path: str


class Repeating(BaseTool[Args]):
    name = "read_file"
    description = "Read a file."
    arguments_type = Args
    approval = ToolApproval.NOT_REQUIRED

    def __init__(self, outputs: list[str] | None = None) -> None:
        self.runs = 0
        self.outputs = outputs

    async def run(self, arguments, invocation=None):
        del invocation
        self.runs += 1
        if self.outputs is None:
            return {"content": "identical"}
        return {"content": self.outputs[min(self.runs - 1, len(self.outputs) - 1)]}


class StuckClient:
    def __init__(self, *, honours_tools: bool = True, path: str = "a.py") -> None:
        self.calls = 0
        self.honours_tools = honours_tools
        self.path = path
        self.tool_offers: list[bool] = []

    async def chat_completion(self, messages, stream=True, tools=None):
        del messages, stream
        self.calls += 1
        self.tool_offers.append(tools is not None)
        if tools is None and self.honours_tools:
            yield StreamEvent(
                type=EventType.MESSAGE_COMPLETE,
                text_delta=TextDelta("I could not determine that."),
            )
            return
        yield StreamEvent(
            type=EventType.MESSAGE_COMPLETE,
            tool_calls=(
                ToolCall(f"call_{self.calls}", "read_file", f'{{"path": "{self.path}"}}'),
            ),
        )

    async def close(self) -> None:
        return


class VaryingClient(StuckClient):
    async def chat_completion(self, messages, stream=True, tools=None):
        del messages, stream
        self.calls += 1
        self.tool_offers.append(tools is not None)
        if tools is None:
            yield StreamEvent(
                type=EventType.MESSAGE_COMPLETE,
                text_delta=TextDelta("done"),
            )
            return
        yield StreamEvent(
            type=EventType.MESSAGE_COMPLETE,
            tool_calls=(
                ToolCall(
                    f"call_{self.calls}",
                    "read_file",
                    f'{{"path": "file{self.calls}.py"}}',
                ),
            ),
        )


async def _approve(request):
    del request
    return ApprovalResponse.approve(ApprovalScope.SESSION)


def _agent(client, tool, max_iterations: int = 25) -> Agent:
    registry = ToolRegistry()
    registry.register(tool)
    return Agent(
        llm_client=client,
        tool_registry=registry,
        approval_handler=_approve,
        max_iterations=max_iterations,
        context_builder=ContextBuilder(
            system_prompt="test system",
            max_input_tokens=100_000,
            token_counter=FixedTokenCounter(),
        ),
    )


class LoopDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_repeated_identical_call_stops_the_loop_early(self):
        client = StuckClient()
        tool = Repeating()
        agent = _agent(client, tool)

        events = [event async for event in agent.run("read a.py")]

        self.assertEqual(tool.runs, 3)
        self.assertLess(client.calls, 6)
        self.assertTrue(
            any(e.type is AgentEventType.PROGRESS_STALLED for e in events)
        )

    async def test_the_user_still_receives_an_answer(self):
        agent = _agent(StuckClient(), Repeating())

        events = [event async for event in agent.run("read a.py")]

        ends = [e for e in events if e.type is AgentEventType.AGENT_END]
        self.assertEqual(len(ends), 1)
        self.assertIn("could not determine", ends[0].data["response"])

    async def test_tools_are_withdrawn_after_a_stall(self):
        client = StuckClient()
        agent = _agent(client, Repeating())

        [event async for event in agent.run("read a.py")]

        self.assertTrue(client.tool_offers[0])
        self.assertFalse(client.tool_offers[-1])

    async def test_the_stall_notice_explains_the_repetition(self):
        agent = _agent(StuckClient(), Repeating())

        events = [event async for event in agent.run("read a.py")]

        stall = next(e for e in events if e.type is AgentEventType.PROGRESS_STALLED)
        self.assertIn("read_file", stall.data["notice"])
        self.assertEqual(stall.data["repeats"], 3)

    async def test_a_model_that_ignores_the_withdrawal_is_stopped(self):
        client = StuckClient(honours_tools=False)
        tool = Repeating()
        agent = _agent(client, tool)

        events = [event async for event in agent.run("read a.py")]

        self.assertEqual(tool.runs, 3)
        errors = [e for e in events if e.type is AgentEventType.AGENT_ERROR]
        self.assertEqual(len(errors), 1)
        self.assertIn("without making progress", errors[0].data["error"])

    async def test_genuine_progress_is_never_interrupted(self):
        client = VaryingClient()
        tool = Repeating()
        agent = _agent(client, tool, max_iterations=8)

        events = [event async for event in agent.run("read the tree")]

        self.assertEqual(tool.runs, 8)
        self.assertFalse(
            any(e.type is AgentEventType.PROGRESS_STALLED for e in events)
        )

    async def test_a_changing_result_survives_the_identical_threshold(self):
        client = StuckClient()
        tool = Repeating(outputs=[f"attempt {index}" for index in range(10)])
        agent = _agent(client, tool)

        events = [event async for event in agent.run("read a.py")]

        stall = next(e for e in events if e.type is AgentEventType.PROGRESS_STALLED)
        self.assertEqual(stall.data["repeats"], 6)
        self.assertIn("without reaching an answer", stall.data["notice"])

    async def test_the_repeated_calls_stay_in_history(self):
        agent = _agent(StuckClient(), Repeating())

        [event async for event in agent.run("read a.py")]

        turn = agent.state.completed_turns[0]
        self.assertEqual(sum(1 for m in turn if m["role"] == "tool"), 3)


if __name__ == "__main__":
    unittest.main()
