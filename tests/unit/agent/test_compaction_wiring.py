import unittest

from tests.unit.agent.test_agent import FixedTokenCounter, ScriptedLLMClient
from truecoder.agent import Agent, AgentState, ContextBuilder
from truecoder.agent.compaction import Compaction, TurnSummarizer
from truecoder.client.response import EventType, StreamEvent, TextDelta


def _reply(text: str) -> list[StreamEvent]:
    return [
        StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta(text)),
        StreamEvent(type=EventType.MESSAGE_COMPLETE),
    ]


class RecordingSummarizer(TurnSummarizer):
    def __init__(self, result: Compaction | None) -> None:
        self.result = result
        self.calls: list[int] = []

    async def summarize(self, turns, previous=None):
        del previous
        self.calls.append(len(turns))
        return self.result


def _state(turns: int) -> AgentState:
    state = AgentState()
    for index in range(turns):
        state.begin_turn(f"q{index}")
        state.complete_turn(f"a{index}")
    return state


def _agent(state: AgentState, summarizer, budget: int = 1) -> Agent:
    return Agent(
        llm_client=ScriptedLLMClient([_reply("ok")]),
        state=state,
        context_builder=ContextBuilder(
            system_prompt="test system",
            max_input_tokens=budget,
            token_counter=FixedTokenCounter(),
        ),
        summarizer=summarizer,
    )


class CompactionWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_long_history_is_compacted_before_the_next_turn(self):
        state = _state(6)
        summarizer = RecordingSummarizer(Compaction(summary="earlier", turn_count=4))
        agent = _agent(state, summarizer)

        [event async for event in agent.run("next")]

        self.assertEqual(summarizer.calls, [4])
        assert state.compaction is not None
        self.assertEqual(state.compaction.summary, "earlier")

    async def test_a_short_history_is_left_alone(self):
        state = _state(2)
        summarizer = RecordingSummarizer(Compaction(summary="earlier", turn_count=1))
        agent = _agent(state, summarizer, budget=100_000)

        [event async for event in agent.run("next")]

        self.assertEqual(summarizer.calls, [])
        self.assertIsNone(state.compaction)

    async def test_no_summarizer_means_no_compaction(self):
        state = _state(6)
        agent = _agent(state, None)

        [event async for event in agent.run("next")]

        self.assertIsNone(state.compaction)

    async def test_a_failed_summary_leaves_history_intact(self):
        state = _state(6)
        summarizer = RecordingSummarizer(None)
        agent = _agent(state, summarizer)

        [event async for event in agent.run("next")]

        self.assertIsNone(state.compaction)
        self.assertEqual(len(state.completed_turns), 7)

    async def test_a_raising_summarizer_never_breaks_the_turn(self):
        class Exploding(TurnSummarizer):
            def __init__(self) -> None:
                pass

            async def summarize(self, turns, previous=None):
                raise RuntimeError("summariser is broken")

        state = _state(6)
        agent = _agent(state, Exploding())

        events = [event async for event in agent.run("next")]

        self.assertIsNone(state.compaction)
        self.assertTrue(any(e.type.value == "agent_end" for e in events))

    async def test_the_compacted_summary_reaches_the_next_request(self):
        state = _state(6)
        summarizer = RecordingSummarizer(Compaction(summary="earlier", turn_count=4))
        client = ScriptedLLMClient([_reply("ok")])
        agent = Agent(
            llm_client=client,
            state=state,
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1,
                token_counter=FixedTokenCounter(),
            ),
            summarizer=summarizer,
        )

        [event async for event in agent.run("next")]

        contents = [m["content"] for m in client.calls[0]["messages"]]
        self.assertTrue(any("earlier" in (c or "") for c in contents))

    def test_a_non_summarizer_is_rejected(self):
        with self.assertRaises(TypeError):
            Agent(
                llm_client=ScriptedLLMClient([]),
                context_builder=ContextBuilder(
                    system_prompt="s",
                    max_input_tokens=10,
                    token_counter=FixedTokenCounter(),
                ),
                summarizer=object(),  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
