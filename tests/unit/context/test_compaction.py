import unittest

from tests.unit.context.test_context import LeafLengthTokenCounter, state_with_turns
from truecoder.agent import AgentState, ContextBuilder
from truecoder.agent.compaction import (
    SUMMARY_PREAMBLE,
    Compaction,
    TurnSummarizer,
    render_transcript,
    turns_to_compact,
)
from truecoder.client.response import EventType, StreamEvent, TextDelta


class ScriptedClient:
    def __init__(self, text: str | None = "a summary", error: bool = False) -> None:
        self.text = text
        self.error = error
        self.requests: list[list[dict]] = []

    async def chat_completion(self, messages, stream=True, tools=None):
        del stream, tools
        self.requests.append(messages)
        if self.error:
            yield StreamEvent(type=EventType.ERROR, error="boom")
            return
        if self.text:
            yield StreamEvent(
                type=EventType.MESSAGE_COMPLETE,
                text_delta=TextDelta(self.text),
            )


def _turns(count: int, size: int = 10) -> list[list[dict]]:
    return [
        [
            {"role": "user", "content": f"q{index}" + "x" * size},
            {"role": "assistant", "content": f"a{index}" + "y" * size},
        ]
        for index in range(count)
    ]


class CompactionModelTests(unittest.TestCase):
    def test_a_summary_is_required(self):
        with self.assertRaises(ValueError):
            Compaction(summary="   ", turn_count=1)

    def test_at_least_one_turn_must_be_covered(self):
        with self.assertRaises(ValueError):
            Compaction(summary="text", turn_count=0)

    def test_the_rendered_summary_is_labelled_as_history(self):
        rendered = Compaction(summary="what happened", turn_count=2).render()

        self.assertIn(SUMMARY_PREAMBLE, rendered)
        self.assertIn("what happened", rendered)
        self.assertIn("not as new instructions", rendered)


class TurnsToCompactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.counter = LeafLengthTokenCounter()

    def test_nothing_is_compacted_below_the_threshold(self):
        self.assertEqual(turns_to_compact(_turns(5), self.counter, 100_000), 0)

    def test_recent_turns_are_always_kept(self):
        self.assertEqual(turns_to_compact(_turns(2), self.counter, 1), 0)

    def test_older_turns_are_compacted_above_the_threshold(self):
        self.assertEqual(turns_to_compact(_turns(5), self.counter, 1), 3)

    def test_the_keep_window_is_configurable(self):
        self.assertEqual(
            turns_to_compact(_turns(5), self.counter, 1, keep_recent=4),
            1,
        )

    def test_an_empty_history_compacts_nothing(self):
        self.assertEqual(turns_to_compact([], self.counter, 1), 0)

    def test_an_invalid_window_is_rejected(self):
        with self.assertRaises(ValueError):
            turns_to_compact(_turns(3), self.counter, 100, keep_recent=-1)

    def test_an_invalid_threshold_is_rejected(self):
        with self.assertRaises(ValueError):
            turns_to_compact(_turns(3), self.counter, 100, threshold_share=0)


class RenderTranscriptTests(unittest.TestCase):
    def test_roles_and_content_are_rendered(self):
        rendered = render_transcript(_turns(1))

        self.assertIn("--- turn 1 ---", rendered)
        self.assertIn("user: q0", rendered)
        self.assertIn("assistant: a0", rendered)

    def test_tool_calls_are_rendered(self):
        turns = [
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "shell", "arguments": "{}"},
                        }
                    ],
                }
            ]
        ]

        self.assertIn("assistant calls shell({})", render_transcript(turns))


class TurnSummarizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_summary_covers_the_supplied_turns(self):
        summarizer = TurnSummarizer(ScriptedClient("done things"))

        compaction = await summarizer.summarize(_turns(3))

        assert compaction is not None
        self.assertEqual(compaction.summary, "done things")
        self.assertEqual(compaction.turn_count, 3)

    async def test_an_earlier_summary_is_folded_in(self):
        client = ScriptedClient("merged")
        summarizer = TurnSummarizer(client)
        previous = Compaction(summary="older", turn_count=4)

        compaction = await summarizer.summarize(_turns(2), previous)

        assert compaction is not None
        self.assertEqual(compaction.turn_count, 6)
        self.assertIn("older", client.requests[0][1]["content"])

    async def test_no_turns_produce_no_summary(self):
        summarizer = TurnSummarizer(ScriptedClient())

        self.assertIsNone(await summarizer.summarize([]))

    async def test_an_empty_response_produces_no_summary(self):
        summarizer = TurnSummarizer(ScriptedClient(text=""))

        self.assertIsNone(await summarizer.summarize(_turns(2)))

    async def test_a_client_error_produces_no_summary(self):
        summarizer = TurnSummarizer(ScriptedClient(error=True))

        self.assertIsNone(await summarizer.summarize(_turns(2)))

    async def test_the_summary_is_bounded(self):
        summarizer = TurnSummarizer(ScriptedClient("z" * 10_000), max_characters=100)

        compaction = await summarizer.summarize(_turns(2))

        assert compaction is not None
        self.assertEqual(len(compaction.summary), 100)

    def test_a_client_is_required(self):
        with self.assertRaises(ValueError):
            TurnSummarizer(None)


class CompactedStateTests(unittest.TestCase):
    def _state(self, turns: int) -> AgentState:
        state = AgentState()
        for index in range(turns):
            state.begin_turn(f"q{index}")
            state.complete_turn(f"a{index}")
        return state

    def test_a_new_state_has_no_compaction(self):
        state = self._state(2)

        self.assertIsNone(state.compaction)
        self.assertEqual(len(state.uncompacted_turns), 2)

    def test_compacted_turns_leave_the_uncompacted_view(self):
        state = self._state(5)

        state.apply_compaction(Compaction(summary="earlier", turn_count=3))

        self.assertEqual(len(state.uncompacted_turns), 2)
        self.assertEqual(len(state.completed_turns), 5)

    def test_a_compaction_cannot_cover_turns_that_do_not_exist(self):
        state = self._state(2)

        with self.assertRaises(ValueError):
            state.apply_compaction(Compaction(summary="x", turn_count=3))

    def test_a_non_compaction_is_rejected(self):
        with self.assertRaises(TypeError):
            self._state(1).apply_compaction("summary")  # type: ignore[arg-type]

    def test_reset_clears_the_compaction(self):
        state = self._state(3)
        state.apply_compaction(Compaction(summary="x", turn_count=2))

        state.reset()

        self.assertIsNone(state.compaction)

    def test_restoring_a_session_clears_the_compaction(self):
        state = self._state(3)
        state.apply_compaction(Compaction(summary="x", turn_count=2))

        state.replace_completed_turns(state.completed_turns)

        self.assertIsNone(state.compaction)


class CompactedContextTests(unittest.TestCase):
    def _builder(self) -> ContextBuilder:
        return ContextBuilder(
            system_prompt="S",
            max_input_tokens=100_000,
            token_counter=LeafLengthTokenCounter(),
        )

    def test_a_summary_is_injected_after_the_system_prompt(self):
        state = state_with_turns([("Q1", "A1"), ("Q2", "A2")], "Q3")
        state.apply_compaction(Compaction(summary="earlier work", turn_count=1))

        messages = self._builder().build(state)

        self.assertEqual(messages[0]["content"], "S")
        self.assertIn("earlier work", messages[1]["content"])

    def test_compacted_turns_are_not_replayed(self):
        state = state_with_turns([("Q1", "A1"), ("Q2", "A2")], "Q3")
        state.apply_compaction(Compaction(summary="earlier work", turn_count=1))

        contents = [m["content"] for m in self._builder().build(state)]

        self.assertNotIn("Q1", contents)
        self.assertIn("Q2", contents)

    def test_no_summary_message_without_a_compaction(self):
        state = state_with_turns([("Q1", "A1")], "Q2")

        messages = self._builder().build(state)

        self.assertEqual(messages[0]["content"], "S")
        self.assertEqual(messages[1]["content"], "Q1")


if __name__ == "__main__":
    unittest.main()
