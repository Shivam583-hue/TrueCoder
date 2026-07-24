import asyncio
import unittest

from truecoder.agent import Agent, AgentEventType, AgentState, ContextBuilder
from truecoder.client.response import (
    EventType,
    StreamEvent,
    TextDelta,
    TokenUsage,
)


class FixedTokenCounter:
    def count_message(self, message) -> int:
        return 1


class FakeLLMClient:
    def __init__(self, events: list[StreamEvent]) -> None:
        self.events = events
        self.calls: list[tuple[list[dict], bool]] = []
        self.closed = False

    async def chat_completion(self, messages, stream=True):
        self.calls.append((messages, stream))
        for event in self.events:
            yield event

    async def close(self) -> None:
        self.closed = True


class FailingLLMClient(FakeLLMClient):
    async def chat_completion(self, messages, stream=True):
        self.calls.append((messages, stream))
        if False:
            yield
        raise RuntimeError("broken client")


class BlockingLLMClient(FakeLLMClient):
    async def chat_completion(self, messages, stream=True):
        self.calls.append((messages, stream))
        yield StreamEvent(
            type=EventType.TEXT_DELTA,
            text_delta=TextDelta("Partial"),
        )
        await asyncio.Event().wait()


def make_agent(
    client: FakeLLMClient,
    state: AgentState | None = None,
) -> Agent:
    return Agent(
        llm_client=client,
        state=state,
        context_builder=ContextBuilder(
            system_prompt="test system",
            max_input_tokens=100,
            token_counter=FixedTokenCounter(),
        ),
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


if __name__ == "__main__":
    unittest.main()
