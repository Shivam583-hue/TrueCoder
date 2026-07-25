import asyncio
import json
import unittest

from truecoder.agent import Agent, AgentEventType, AgentState, ContextBuilder
from truecoder.client.response import (
    EventType,
    StreamEvent,
    TextDelta,
    TokenUsage,
)
from truecoder.tools import (
    ToolApproval,
    ToolArguments,
    ToolCall,
    ToolRegistry,
    ToolResult,
    serialize_tool_result,
)
from truecoder.tools.base import BaseTool


class EchoArguments(ToolArguments):
    text: str


class EchoTool(BaseTool[EchoArguments]):
    name = "echo"
    description = "Echo the provided text back to the caller."
    arguments_type = EchoArguments
    approval = ToolApproval.NOT_REQUIRED

    async def run(self, arguments: EchoArguments) -> dict[str, str]:
        return {"echoed": arguments.text}


def echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


async def collect(agent: Agent, prompt: str) -> list:
    return [event async for event in agent.run(prompt)]


class FixedTokenCounter:
    def count_message(self, message) -> int:
        return 1


class FakeLLMClient:
    def __init__(self, events: list[StreamEvent]) -> None:
        self.events = events
        self.calls: list[tuple[list[dict], bool]] = []
        self.tools_sent: list[list[dict] | None] = []
        self.closed = False

    async def chat_completion(self, messages, stream=True, tools=None):
        self.calls.append((messages, stream))
        self.tools_sent.append(tools)
        for event in self.events:
            yield event

    async def close(self) -> None:
        self.closed = True


class FailingLLMClient(FakeLLMClient):
    async def chat_completion(self, messages, stream=True, tools=None):
        self.calls.append((messages, stream))
        if False:
            yield
        raise RuntimeError("broken client")


class BlockingLLMClient(FakeLLMClient):
    async def chat_completion(self, messages, stream=True, tools=None):
        self.calls.append((messages, stream))
        yield StreamEvent(
            type=EventType.TEXT_DELTA,
            text_delta=TextDelta("Partial"),
        )
        await asyncio.Event().wait()


class ScriptedLLMClient:
    """Yields a scripted batch of stream events on each successive call."""

    def __init__(self, batches: list[list[StreamEvent]]) -> None:
        self.batches = batches
        self.calls: list[dict] = []
        self.closed = False

    async def chat_completion(self, messages, stream=True, tools=None):
        index = len(self.calls)
        self.calls.append({"messages": messages, "stream": stream, "tools": tools})
        batch = self.batches[index] if index < len(self.batches) else []
        for event in batch:
            yield event

    async def close(self) -> None:
        self.closed = True


def make_agent(
    client,
    state: AgentState | None = None,
    tool_registry: ToolRegistry | None = None,
    max_iterations: int = 25,
) -> Agent:
    return Agent(
        llm_client=client,
        state=state,
        context_builder=ContextBuilder(
            system_prompt="test system",
            max_input_tokens=100,
            token_counter=FixedTokenCounter(),
        ),
        tool_registry=tool_registry,
        max_iterations=max_iterations,
    )


class AgentTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def collect(agent: Agent, prompt: str):
        return [event async for event in agent.run(prompt)]

    async def test_successful_turn_streams_events_and_updates_history(self):
        usage = TokenUsage(
            prompt_tokens=2,
            completion_tokens=3,
            total_tokens=5,
            cached_tokens=1,
        )
        client = FakeLLMClient(
            [
                StreamEvent(
                    type=EventType.TEXT_DELTA,
                    text_delta=TextDelta("Hello "),
                ),
                StreamEvent(
                    type=EventType.TEXT_DELTA,
                    text_delta=TextDelta("world"),
                ),
                StreamEvent(
                    type=EventType.MESSAGE_COMPLETE,
                    usage=usage,
                    finish_reason="stop",
                ),
            ]
        )
        agent = make_agent(client)

        events = await self.collect(agent, "  Say hello  ")

        self.assertEqual(
            [event.type for event in events],
            [
                AgentEventType.AGENT_START,
                AgentEventType.TEXT_DELTA,
                AgentEventType.TEXT_DELTA,
                AgentEventType.TEXT_COMPLETE,
                AgentEventType.AGENT_END,
            ],
        )
        self.assertEqual(events[0].data["message"], "Say hello")
        self.assertEqual(events[-2].data["content"], "Hello world")
        self.assertEqual(events[-1].data["response"], "Hello world")
        self.assertEqual(events[-1].data["usage"]["total_tokens"], 5)
        self.assertEqual(events[-1].data["finish_reason"], "stop")
        self.assertEqual(
            agent.messages,
            [
                {"role": "user", "content": "Say hello"},
                {"role": "assistant", "content": "Hello world"},
            ],
        )
        self.assertEqual(
            client.calls,
            [
                (
                    [
                        {"role": "system", "content": "test system"},
                        {"role": "user", "content": "Say hello"},
                    ],
                    True,
                )
            ],
        )
        self.assertFalse(agent.state.turn_active)

    async def test_next_turn_receives_completed_conversation_history(self):
        client = FakeLLMClient(
            [
                StreamEvent(
                    type=EventType.TEXT_DELTA,
                    text_delta=TextDelta("Answer"),
                ),
                StreamEvent(type=EventType.MESSAGE_COMPLETE),
            ]
        )
        agent = make_agent(client)

        await self.collect(agent, "First")
        await self.collect(agent, "Second")

        self.assertEqual(
            client.calls[-1],
            (
                [
                    {"role": "system", "content": "test system"},
                    {"role": "user", "content": "First"},
                    {"role": "assistant", "content": "Answer"},
                    {"role": "user", "content": "Second"},
                ],
                True,
            ),
        )

    async def test_client_error_aborts_pending_turn(self):
        client = FakeLLMClient(
            [
                StreamEvent(
                    type=EventType.ERROR,
                    error="Connection error: offline",
                )
            ]
        )
        agent = make_agent(client)

        events = await self.collect(agent, "Hello?")

        self.assertEqual(
            [event.type for event in events],
            [AgentEventType.AGENT_START, AgentEventType.AGENT_ERROR],
        )
        self.assertEqual(events[-1].data["error"], "Connection error: offline")
        self.assertEqual(agent.messages, [])
        self.assertFalse(agent.state.turn_active)

    async def test_unexpected_client_exception_aborts_pending_turn(self):
        agent = make_agent(FailingLLMClient([]))

        events = await self.collect(agent, "Hello?")

        self.assertEqual(events[-1].type, AgentEventType.AGENT_ERROR)
        self.assertEqual(events[-1].data["error"], "broken client")
        self.assertEqual(
            events[-1].data["details"]["exception_type"],
            "RuntimeError",
        )
        self.assertEqual(agent.messages, [])
        self.assertFalse(agent.state.turn_active)

    async def test_incomplete_stream_aborts_pending_turn(self):
        agent = make_agent(FakeLLMClient([]))

        events = await self.collect(agent, "Hello?")

        self.assertEqual(events[-1].type, AgentEventType.AGENT_ERROR)
        self.assertIn("before completion", events[-1].data["error"])
        self.assertEqual(agent.messages, [])
        self.assertFalse(agent.state.turn_active)

    async def test_completion_without_text_aborts_pending_turn(self):
        agent = make_agent(
            FakeLLMClient([StreamEvent(type=EventType.MESSAGE_COMPLETE)])
        )

        events = await self.collect(agent, "Hello?")

        self.assertEqual(events[-1].type, AgentEventType.AGENT_ERROR)
        self.assertIn("without returning any text", events[-1].data["error"])
        self.assertEqual(agent.messages, [])
        self.assertFalse(agent.state.turn_active)

    async def test_empty_prompt_is_rejected_without_calling_client(self):
        client = FakeLLMClient([])
        agent = make_agent(client)

        events = await self.collect(agent, "   ")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, AgentEventType.AGENT_ERROR)
        self.assertEqual(client.calls, [])
        self.assertEqual(agent.messages, [])

    async def test_cancellation_aborts_pending_turn(self):
        agent = make_agent(BlockingLLMClient([]))
        stream = agent.run("Long request")

        self.assertEqual((await anext(stream)).type, AgentEventType.AGENT_START)
        self.assertEqual((await anext(stream)).type, AgentEventType.TEXT_DELTA)

        pending_event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        pending_event.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await pending_event

        self.assertEqual(agent.messages, [])
        self.assertFalse(agent.state.turn_active)

    async def test_reset_and_close_delegate_to_owned_state(self):
        client = FakeLLMClient([])
        state = AgentState()
        state.begin_turn("old question")
        state.complete_turn("old answer")
        agent = make_agent(client, state)

        agent.reset()
        await agent.close()

        self.assertEqual(agent.messages, [])
        self.assertTrue(client.closed)


class AgentToolLoopTests(unittest.IsolatedAsyncioTestCase):
    def test_agent_rejects_invalid_iteration_limit(self):
        with self.assertRaises(TypeError):
            make_agent(FakeLLMClient([]), max_iterations=True)
        with self.assertRaises(ValueError):
            make_agent(FakeLLMClient([]), max_iterations=0)

    async def test_tool_call_round_trip_executes_and_feeds_result_back(self):
        client = ScriptedLLMClient(
            [
                [
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        tool_calls=(
                            ToolCall("call_1", "echo", '{"text": "hi"}'),
                        ),
                        finish_reason="tool_calls",
                    ),
                ],
                [
                    StreamEvent(
                        type=EventType.TEXT_DELTA,
                        text_delta=TextDelta("Done"),
                    ),
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        finish_reason="stop",
                    ),
                ],
            ]
        )
        agent = make_agent(client, tool_registry=echo_registry())

        events = await collect(agent, "echo hi")

        self.assertEqual(
            [event.type for event in events],
            [
                AgentEventType.AGENT_START,
                AgentEventType.TOOL_CALL,
                AgentEventType.TOOL_RESULT,
                AgentEventType.TEXT_DELTA,
                AgentEventType.TEXT_COMPLETE,
                AgentEventType.AGENT_END,
            ],
        )

        tool_call_event = events[1]
        self.assertEqual(tool_call_event.data["name"], "echo")
        self.assertEqual(tool_call_event.data["arguments"], '{"text": "hi"}')

        tool_result_event = events[2]
        self.assertEqual(tool_result_event.data["status"], "success")

        expected_content = serialize_tool_result(
            ToolResult.success("call_1", "echo", {"echoed": "hi"})
        )
        self.assertEqual(
            agent.messages,
            [
                {"role": "user", "content": "echo hi"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "echo",
                                "arguments": '{"text": "hi"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": expected_content,
                },
                {"role": "assistant", "content": "Done"},
            ],
        )

        # The second request carried the recorded tool result back to the model.
        second_request_messages = client.calls[1]["messages"]
        self.assertEqual(second_request_messages[-1]["role"], "tool")

    async def test_multiple_tool_rounds_before_final_answer(self):
        client = ScriptedLLMClient(
            [
                [
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        tool_calls=(ToolCall("call_1", "echo", '{"text": "a"}'),),
                        finish_reason="tool_calls",
                    ),
                ],
                [
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        tool_calls=(ToolCall("call_2", "echo", '{"text": "b"}'),),
                        finish_reason="tool_calls",
                    ),
                ],
                [
                    StreamEvent(
                        type=EventType.TEXT_DELTA,
                        text_delta=TextDelta("All done"),
                    ),
                    StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop"),
                ],
            ]
        )
        agent = make_agent(client, tool_registry=echo_registry())

        await collect(agent, "run tools")

        self.assertEqual(len(client.calls), 3)
        self.assertEqual(
            [message["role"] for message in agent.messages],
            ["user", "assistant", "tool", "assistant", "tool", "assistant"],
        )

    async def test_tool_definitions_are_sent_to_the_model(self):
        client = ScriptedLLMClient(
            [
                [
                    StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta("Hi")),
                    StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop"),
                ]
            ]
        )
        agent = make_agent(client, tool_registry=echo_registry())

        await collect(agent, "hello")

        tools = client.calls[0]["tools"]
        self.assertIsNotNone(tools)
        self.assertEqual(tools[0]["function"]["name"], "echo")

    async def test_no_registered_tools_sends_no_tool_definitions(self):
        client = ScriptedLLMClient(
            [
                [
                    StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta("Hi")),
                    StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop"),
                ]
            ]
        )
        agent = make_agent(client)

        await collect(agent, "hello")

        self.assertIsNone(client.calls[0]["tools"])

    async def test_unknown_tool_returns_error_result_and_recovers(self):
        client = ScriptedLLMClient(
            [
                [
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        tool_calls=(ToolCall("call_1", "missing_tool", "{}"),),
                        finish_reason="tool_calls",
                    ),
                ],
                [
                    StreamEvent(
                        type=EventType.TEXT_DELTA,
                        text_delta=TextDelta("Recovered"),
                    ),
                    StreamEvent(type=EventType.MESSAGE_COMPLETE, finish_reason="stop"),
                ],
            ]
        )
        agent = make_agent(client, tool_registry=echo_registry())

        events = await collect(agent, "use missing tool")

        tool_results = [
            event for event in events if event.type == AgentEventType.TOOL_RESULT
        ]
        self.assertEqual(len(tool_results), 1)
        self.assertEqual(tool_results[0].data["status"], "error")

        payload = json.loads(tool_results[0].data["content"])
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_code"], "tool_not_found")

        self.assertEqual(events[-1].type, AgentEventType.AGENT_END)
        self.assertEqual(
            agent.messages[-1],
            {"role": "assistant", "content": "Recovered"},
        )

    async def test_iteration_limit_aborts_turn_with_error(self):
        client = ScriptedLLMClient(
            [
                [
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        tool_calls=(ToolCall("call_1", "echo", '{"text": "a"}'),),
                        finish_reason="tool_calls",
                    ),
                ],
                [
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        tool_calls=(ToolCall("call_2", "echo", '{"text": "b"}'),),
                        finish_reason="tool_calls",
                    ),
                ],
                [
                    StreamEvent(
                        type=EventType.MESSAGE_COMPLETE,
                        tool_calls=(ToolCall("call_3", "echo", '{"text": "c"}'),),
                        finish_reason="tool_calls",
                    ),
                ],
            ]
        )
        agent = make_agent(client, tool_registry=echo_registry(), max_iterations=2)

        events = await collect(agent, "loop forever")

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(events[-1].type, AgentEventType.AGENT_ERROR)
        self.assertIn("model requests", events[-1].data["error"])
        self.assertFalse(agent.state.turn_active)
        self.assertEqual(agent.messages, [])


if __name__ == "__main__":
    unittest.main()
