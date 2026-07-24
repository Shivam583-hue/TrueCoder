import os
import unittest
from unittest.mock import Mock, patch

from truecoder.agent import AgentState, ContextBuilder, TiktokenTokenCounter
from truecoder.agent.prompts import DEFAULT_SYSTEM_PROMPT


class LengthTokenCounter:
    def count_message(self, message) -> int:
        return len(message["content"])


def state_with_turns(
    completed_turns: list[tuple[str, str]],
    pending_prompt: str,
) -> AgentState:
    state = AgentState()
    for prompt, response in completed_turns:
        state.begin_turn(prompt)
        state.complete_turn(response)
    state.begin_turn(pending_prompt)
    return state


class ContextBuilderTests(unittest.TestCase):
    def make_builder(self, max_input_tokens: int = 100) -> ContextBuilder:
        return ContextBuilder(
            system_prompt="S",
            max_input_tokens=max_input_tokens,
            token_counter=LengthTokenCounter(),
        )

    def test_constructor_normalizes_system_prompt(self):
        builder = ContextBuilder(
            system_prompt="  system instructions  ",
            max_input_tokens=10,
            token_counter=LengthTokenCounter(),
        )

        self.assertEqual(builder.system_prompt, "system instructions")

    def test_constructor_rejects_invalid_configuration(self):
        valid_counter = LengthTokenCounter()
        invalid_cases = [
            (
                {"system_prompt": "", "max_input_tokens": 10, "token_counter": valid_counter},
                ValueError,
            ),
            (
                {
                    "system_prompt": "S",
                    "max_input_tokens": True,
                    "token_counter": valid_counter,
                },
                TypeError,
            ),
            (
                {
                    "system_prompt": "S",
                    "max_input_tokens": 0,
                    "token_counter": valid_counter,
                },
                ValueError,
            ),
            (
                {"system_prompt": "S", "max_input_tokens": 10, "token_counter": None},
                ValueError,
            ),
        ]

        for arguments, expected_error in invalid_cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(expected_error):
                    ContextBuilder(**arguments)

    def test_build_requires_an_active_turn(self):
        with self.assertRaisesRegex(RuntimeError, "without an active turn"):
            self.make_builder().build(AgentState())

    def test_build_orders_system_history_and_current_prompt(self):
        state = state_with_turns(
            [("First question", "First answer"), ("Second question", "Second answer")],
            "Current question",
        )

        messages = self.make_builder().build(state)

        self.assertEqual(
            messages,
            [
                {"role": "system", "content": "S"},
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "First answer"},
                {"role": "user", "content": "Second question"},
                {"role": "assistant", "content": "Second answer"},
                {"role": "user", "content": "Current question"},
            ],
        )

    def test_build_removes_oldest_complete_pairs_first(self):
        state = state_with_turns(
            [("a", "A"), ("bb", "BB")],
            "C",
        )

        messages = self.make_builder(max_input_tokens=6).build(state)

        self.assertEqual(
            messages,
            [
                {"role": "system", "content": "S"},
                {"role": "user", "content": "bb"},
                {"role": "assistant", "content": "BB"},
                {"role": "user", "content": "C"},
            ],
        )

    def test_build_keeps_history_contiguous_when_newest_pair_does_not_fit(self):
        state = state_with_turns(
            [("a", "A"), ("long", "LONG")],
            "C",
        )

        messages = self.make_builder(max_input_tokens=6).build(state)

        self.assertEqual(
            messages,
            [
                {"role": "system", "content": "S"},
                {"role": "user", "content": "C"},
            ],
        )

    def test_build_keeps_required_messages_when_they_exceed_budget(self):
        state = state_with_turns([], "current")
        builder = ContextBuilder(
            system_prompt="system",
            max_input_tokens=1,
            token_counter=LengthTokenCounter(),
        )

        messages = builder.build(state)

        self.assertEqual(
            messages,
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "current"},
            ],
        )

    def test_build_returns_messages_independent_of_state(self):
        state = state_with_turns([("Question", "Answer")], "Current")

        messages = self.make_builder().build(state)
        messages[1]["content"] = "changed"

        self.assertEqual(
            state.messages[0],
            {"role": "user", "content": "Question"},
        )

    def test_from_environment_builds_default_configuration(self):
        counter = Mock()

        with (
            patch("truecoder.agent.context.load_dotenv"),
            patch.dict(
                os.environ,
                {"MODEL": "  test-model  ", "MAX_INPUT_TOKENS": "42"},
                clear=True,
            ),
            patch(
                "truecoder.agent.context.TiktokenTokenCounter",
                return_value=counter,
            ) as counter_type,
        ):
            builder = ContextBuilder.from_environment()

        self.assertEqual(builder.system_prompt, DEFAULT_SYSTEM_PROMPT.strip())
        self.assertEqual(builder.max_input_tokens, 42)
        self.assertIs(builder.token_counter, counter)
        counter_type.assert_called_once_with("test-model")

    def test_from_environment_defaults_input_limit(self):
        with (
            patch("truecoder.agent.context.load_dotenv"),
            patch.dict(os.environ, {"MODEL": "test-model"}, clear=True),
            patch("truecoder.agent.context.TiktokenTokenCounter"),
        ):
            builder = ContextBuilder.from_environment()

        self.assertEqual(builder.max_input_tokens, 12000)

    def test_from_environment_rejects_missing_model(self):
        with (
            patch("truecoder.agent.context.load_dotenv"),
            patch.dict(os.environ, {}, clear=True),
        ):
            with self.assertRaisesRegex(ValueError, "MODEL"):
                ContextBuilder.from_environment()

    def test_from_environment_rejects_non_integer_limit(self):
        with (
            patch("truecoder.agent.context.load_dotenv"),
            patch.dict(
                os.environ,
                {"MODEL": "test-model", "MAX_INPUT_TOKENS": "many"},
                clear=True,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "valid integer"):
                ContextBuilder.from_environment()


class TiktokenTokenCounterTests(unittest.TestCase):
    def test_unknown_model_uses_fallback_encoding(self):
        encoding = Mock()
        encoding.encode.side_effect = lambda value: list(value)

        with (
            patch(
                "truecoder.agent.context.tiktoken.encoding_for_model",
                side_effect=KeyError,
            ),
            patch(
                "truecoder.agent.context.tiktoken.get_encoding",
                return_value=encoding,
            ) as get_encoding,
        ):
            counter = TiktokenTokenCounter("custom-model")

        self.assertEqual(
            counter.count_message({"role": "user", "content": "hi"}),
            10,
        )
        get_encoding.assert_called_once_with("o200k_base")

    def test_count_message_rejects_invalid_role_or_content(self):
        encoding = Mock()

        with patch(
            "truecoder.agent.context.tiktoken.encoding_for_model",
            return_value=encoding,
        ):
            counter = TiktokenTokenCounter("test-model")

        invalid_messages = [
            {"content": "hello"},
            {"role": "user"},
            {"role": 1, "content": "hello"},
            {"role": "user", "content": None},
        ]
        for message in invalid_messages:
            with self.subTest(message=message):
                with self.assertRaises(TypeError):
                    counter.count_message(message)


if __name__ == "__main__":
    unittest.main()
