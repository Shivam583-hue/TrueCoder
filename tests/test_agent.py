import unittest

from truecoder.agent import Agent, AgentEventType
from truecoder.client.response import (
    EventType,
    StreamEvent,
    TextDelta,
    TokenUsage,
)


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
        agent = Agent(client)

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
            [([{"role": "user", "content": "Say hello"}], True)],
        )

    async def test_client_error_becomes_an_agent_error(self):
        client = FakeLLMClient(
            [
                StreamEvent(
                    type=EventType.ERROR,
                    error="Connection error: offline",
                )
            ]
        )
        agent = Agent(client)

        events = await self.collect(agent, "Hello?")

        self.assertEqual(
            [event.type for event in events],
            [AgentEventType.AGENT_START, AgentEventType.AGENT_ERROR],
        )
        self.assertEqual(events[-1].data["error"], "Connection error: offline")

    async def test_unexpected_client_exception_becomes_an_agent_error(self):
        agent = Agent(FailingLLMClient([]))

        events = await self.collect(agent, "Hello?")

        self.assertEqual(events[-1].type, AgentEventType.AGENT_ERROR)
        self.assertEqual(events[-1].data["error"], "broken client")
        self.assertEqual(
            events[-1].data["details"]["exception_type"],
            "RuntimeError",
        )

    async def test_empty_prompt_is_rejected_without_calling_the_client(self):
        client = FakeLLMClient([])
        agent = Agent(client)

        events = await self.collect(agent, "   ")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, AgentEventType.AGENT_ERROR)
        self.assertEqual(client.calls, [])
        self.assertEqual(agent.messages, [])

    async def test_reset_and_close_delegate_to_owned_state(self):
        client = FakeLLMClient([])
        agent = Agent(client)
        agent.messages.append({"role": "user", "content": "old"})

        agent.reset()
        await agent.close()

        self.assertEqual(agent.messages, [])
        self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
