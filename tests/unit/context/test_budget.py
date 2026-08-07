import json
import unittest

from tests.unit.context.test_context import (
    LeafLengthTokenCounter,
    state_with_turns,
)
from truecoder.agent import AgentState, ContextBuilder
from truecoder.agent.budget import (
    MIN_TOOL_RESULT_TOKENS,
    TRUNCATION_NOTE,
    fit_tool_message,
    fit_tool_messages,
    tool_result_ceiling,
)
from truecoder.agent.messages import create_tool_message, create_user_message
from truecoder.tools import ToolCall


def _result(output) -> str:
    return json.dumps({"status": "success", "output": output})


class ToolResultCeilingTests(unittest.TestCase):
    def test_the_ceiling_is_a_share_of_the_budget(self):
        self.assertEqual(tool_result_ceiling(12000), 3000)

    def test_a_small_budget_still_leaves_a_usable_ceiling(self):
        self.assertEqual(tool_result_ceiling(100), MIN_TOOL_RESULT_TOKENS)

    def test_invalid_budgets_are_rejected(self):
        with self.assertRaises(ValueError):
            tool_result_ceiling(0)
        with self.assertRaises(TypeError):
            tool_result_ceiling("12000")  # type: ignore[arg-type]

    def test_invalid_shares_are_rejected(self):
        with self.assertRaises(ValueError):
            tool_result_ceiling(12000, share=0)
        with self.assertRaises(ValueError):
            tool_result_ceiling(12000, minimum=0)


class FitToolMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.counter = LeafLengthTokenCounter()

    def _fit(self, content: str, ceiling: int):
        return fit_tool_message(create_tool_message("call_1", content), self.counter, ceiling)

    def test_a_message_within_the_ceiling_is_untouched(self):
        message = create_tool_message("call_1", _result("small"))

        self.assertIs(fit_tool_message(message, self.counter, 10_000), message)

    def test_an_oversized_message_is_brought_under_the_ceiling(self):
        fitted = self._fit(_result("x" * 5000), 400)

        self.assertLessEqual(self.counter.count_message(fitted), 400)

    def test_the_truncated_message_is_valid_json(self):
        fitted = self._fit(_result("x" * 5000), 400)

        payload = json.loads(fitted["content"])
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["note"], TRUNCATION_NOTE)

    def test_the_original_status_is_preserved(self):
        fitted = self._fit(
            json.dumps({"status": "error", "error": "y" * 5000}),
            400,
        )

        self.assertEqual(json.loads(fitted["content"])["status"], "error")

    def test_the_omitted_count_is_reported(self):
        fitted = self._fit(_result("x" * 5000), 400)

        payload = json.loads(fitted["content"])
        self.assertGreater(payload["omitted_characters"], 0)
        self.assertEqual(
            payload["omitted_characters"] + len(payload["output"]),
            5000,
        )

    def test_as_much_content_as_fits_is_kept(self):
        small = self._fit(_result("x" * 5000), 400)
        large = self._fit(_result("x" * 5000), 900)

        small_kept = len(json.loads(small["content"])["output"])
        large_kept = len(json.loads(large["content"])["output"])
        self.assertGreater(large_kept, small_kept)

    def test_the_call_id_survives_truncation(self):
        fitted = self._fit(_result("x" * 5000), 400)

        self.assertEqual(fitted["tool_call_id"], "call_1")

    def test_a_structured_output_is_rendered_before_truncation(self):
        fitted = self._fit(_result({"lines": ["a" * 100] * 100}), 400)

        self.assertIn("lines", json.loads(fitted["content"])["output"])

    def test_content_that_is_not_json_is_still_bounded(self):
        fitted = self._fit("plain text " * 2000, 400)

        self.assertLessEqual(self.counter.count_message(fitted), 400)
        self.assertTrue(json.loads(fitted["content"])["truncated"])

    def test_a_ceiling_too_small_for_any_content_still_returns_valid_json(self):
        fitted = self._fit(_result("x" * 5000), 1)

        payload = json.loads(fitted["content"])
        self.assertEqual(payload["output"], "")
        self.assertTrue(payload["truncated"])

    def test_an_invalid_ceiling_is_rejected(self):
        with self.assertRaises(ValueError):
            self._fit(_result("x"), 0)

    def test_the_default_minimum_clears_the_envelope_floor(self):
        floor = self.counter.count_message(self._fit(_result("x" * 5000), 1))

        self.assertLess(floor, MIN_TOOL_RESULT_TOKENS)


class FitToolMessagesTests(unittest.TestCase):
    def test_only_tool_messages_are_touched(self):
        counter = LeafLengthTokenCounter()
        messages = [
            create_user_message("q" * 5000),
            create_tool_message("call_1", _result("x" * 5000)),
        ]

        fitted = fit_tool_messages(messages, counter, 400)  # type: ignore[arg-type]

        self.assertEqual(fitted[0], messages[0])
        self.assertNotEqual(fitted[1], messages[1])


class BudgetedContextTests(unittest.TestCase):
    def _builder(self, **kwargs) -> ContextBuilder:
        return ContextBuilder(
            system_prompt="S",
            max_input_tokens=kwargs.pop("max_input_tokens", 1000),
            token_counter=LeafLengthTokenCounter(),
            **kwargs,
        )

    def _state(self, output: str) -> AgentState:
        state = AgentState()
        state.begin_turn("go")
        state.record_tool_calls([ToolCall("call_1", "shell", "{}")])
        state.record_tool_result("call_1", _result(output))
        return state

    def test_the_default_ceiling_follows_the_budget(self):
        self.assertEqual(self._builder(max_input_tokens=4000).max_tool_result_tokens, 1000)

    def test_an_explicit_ceiling_is_honoured(self):
        builder = self._builder(max_tool_result_tokens=42)

        self.assertEqual(builder.max_tool_result_tokens, 42)

    def test_an_invalid_ceiling_is_rejected(self):
        with self.assertRaises(ValueError):
            self._builder(max_tool_result_tokens=0)

    def test_an_oversized_tool_result_is_bounded_in_the_request(self):
        builder = self._builder(max_tool_result_tokens=400)

        messages = builder.build(self._state("x" * 10_000))

        tool_message = next(m for m in messages if m["role"] == "tool")
        self.assertLessEqual(
            builder.token_counter.count_message(tool_message),
            400,
        )

    def test_the_stored_turn_keeps_the_whole_result(self):
        builder = self._builder(max_tool_result_tokens=400)
        state = self._state("x" * 10_000)

        builder.build(state)

        stored = state.pending_messages[-1]["content"]
        self.assertIn("x" * 10_000, stored)

    def test_history_is_bounded_the_same_way(self):
        builder = self._builder(max_tool_result_tokens=400)
        state = AgentState()
        state.begin_turn("first")
        state.record_tool_calls([ToolCall("call_1", "shell", "{}")])
        state.record_tool_result("call_1", _result("x" * 10_000))
        state.complete_turn("done")
        state.begin_turn("second")

        messages = builder.build(state)

        tool_messages = [m for m in messages if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertLessEqual(
            builder.token_counter.count_message(tool_messages[0]),
            400,
        )

    def test_a_bounded_history_turn_now_fits_where_it_did_not_before(self):
        state = AgentState()
        state.begin_turn("first")
        state.record_tool_calls([ToolCall("call_1", "shell", "{}")])
        state.record_tool_result("call_1", _result("x" * 5000))
        state.complete_turn("done")
        state.begin_turn("second")

        unbounded = self._builder(max_input_tokens=2000, max_tool_result_tokens=100_000)
        bounded = self._builder(max_input_tokens=2000, max_tool_result_tokens=400)

        self.assertNotIn("first", [m["content"] for m in unbounded.build(state)])
        self.assertIn("first", [m["content"] for m in bounded.build(state)])

    def test_user_text_is_never_truncated(self):
        builder = self._builder(max_tool_result_tokens=50)
        prompt = ("a very long question " * 200).strip()
        state = state_with_turns([], prompt)

        messages = builder.build(state)

        self.assertIn(prompt, messages[-1]["content"] or "")


if __name__ == "__main__":
    unittest.main()
