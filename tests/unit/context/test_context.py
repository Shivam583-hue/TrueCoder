import os
import unittest
from collections.abc import Mapping, Sequence
from typing import Any
from unittest.mock import Mock, patch

from truecoder.agent import AgentState, ContextBuilder, TiktokenTokenCounter
from truecoder.agent.prompts import (
    DEFAULT_SYSTEM_PROMPT,
    SHELL_TOOL_GUIDANCE,
    add_shell_tool_guidance,
    build_system_prompt,
)
from truecoder.tools import ToolCall


class LengthTokenCounter:
    def count_message(self, message) -> int:
        return len(message["content"])


class LeafLengthTokenCounter:
    """Count characters across every string leaf, mirroring the real traversal."""

    def count_message(self, message) -> int:
        return self._count(message)

    def _count(self, value) -> int:
        if isinstance(value, str):
            return len(value)
        if value is None:
            return 0
        if isinstance(value, Mapping):
            return sum(self._count(nested) for nested in value.values())
        if isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            return sum(self._count(nested) for nested in value)
        return 0


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
            with self.subTest(arguments=arguments), self.assertRaises(expected_error):
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

    def test_from_environment_adds_project_instructions_to_system_prompt(self):
        with (
            patch("truecoder.agent.context.load_dotenv"),
            patch.dict(os.environ, {"MODEL": "test-model"}, clear=True),
            patch("truecoder.agent.context.TiktokenTokenCounter"),
        ):
            builder = ContextBuilder.from_environment(
                project_instructions="Root guidance\n\nNested guidance",
            )

        self.assertEqual(
            builder.system_prompt,
            build_system_prompt("Root guidance\n\nNested guidance"),
        )
        self.assertEqual(builder.system_prompt.count("<project_instructions>"), 1)

    def test_system_prompt_is_unchanged_without_project_instructions(self):
        self.assertEqual(
            build_system_prompt(),
            DEFAULT_SYSTEM_PROMPT.strip(),
        )
        self.assertEqual(
            build_system_prompt(" \n\t"),
            DEFAULT_SYSTEM_PROMPT.strip(),
        )

    def test_system_prompt_rejects_non_string_project_instructions(self):
        with self.assertRaises(TypeError):
            build_system_prompt(None)  # type: ignore[arg-type]

    def test_shell_guidance_is_added_only_when_enabled(self):
        builder = self.make_builder()

        self.assertNotIn(SHELL_TOOL_GUIDANCE.strip(), builder.system_prompt)

        builder.enable_shell_tool()
        builder.enable_shell_tool()

        self.assertEqual(
            builder.system_prompt.count(SHELL_TOOL_GUIDANCE.strip()),
            1,
        )

    def test_shell_guidance_preserves_project_instructions(self):
        prompt = build_system_prompt("Repository guidance")

        result = add_shell_tool_guidance(prompt)

        self.assertIn("<project_instructions>", result)
        self.assertIn("Repository guidance", result)
        self.assertTrue(result.endswith(SHELL_TOOL_GUIDANCE.strip()))

    def test_shell_guidance_rejects_invalid_prompts(self):
        with self.assertRaises(TypeError):
            add_shell_tool_guidance(None)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            add_shell_tool_guidance(" ")

    def test_project_instructions_participate_in_context_budgeting(self):
        state = state_with_turns([("old", "answer")], "current")
        system_prompt = build_system_prompt("Repository guidance")
        builder = ContextBuilder(
            system_prompt=system_prompt,
            max_input_tokens=len(system_prompt) + len("current"),
            token_counter=LengthTokenCounter(),
        )

        messages = builder.build(state)

        self.assertEqual(
            messages,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "current"},
            ],
        )

    def test_rebuilding_context_does_not_duplicate_project_instructions(self):
        state = AgentState()
        state.begin_turn("Question")
        system_prompt = build_system_prompt("Repository guidance")
        builder = ContextBuilder(
            system_prompt=system_prompt,
            max_input_tokens=10_000,
            token_counter=LengthTokenCounter(),
        )

        first_request = builder.build(state)
        second_request = builder.build(state)

        self.assertEqual(first_request[0], second_request[0])
        self.assertEqual(
            first_request[0]["content"].count("<project_instructions>"),
            1,
        )

    def test_from_environment_defaults_input_limit(self):
        with (
            patch("truecoder.agent.context.load_dotenv"),
            patch.dict(os.environ, {"MODEL": "test-model"}, clear=True),
            patch("truecoder.agent.context.TiktokenTokenCounter"),
        ):
            builder = ContextBuilder.from_environment()

        self.assertEqual(builder.max_input_tokens, 64000)

    def test_from_environment_rejects_missing_model(self):
        with (
            patch("truecoder.agent.context.load_dotenv"),
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValueError, "MODEL"),
        ):
            ContextBuilder.from_environment()

    def test_from_environment_rejects_non_integer_limit(self):
        with (
            patch("truecoder.agent.context.load_dotenv"),
            patch.dict(
                os.environ,
                {"MODEL": "test-model", "MAX_INPUT_TOKENS": "many"},
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "valid integer"),
        ):
            ContextBuilder.from_environment()


def _tool_call(
    call_id: str,
    name: str = "read_file",
    arguments_json: str = '{"path": "main.py"}',
) -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments_json=arguments_json)


class ContextBuilderToolTests(unittest.TestCase):
    def make_builder(self, max_input_tokens: int = 10_000) -> ContextBuilder:
        return ContextBuilder(
            system_prompt="S",
            max_input_tokens=max_input_tokens,
            token_counter=LeafLengthTokenCounter(),
        )

    def test_pending_tool_messages_appear_in_provider_order(self):
        state = AgentState()
        state.begin_turn("Read main.py")
        state.record_tool_calls([_tool_call("call_1")])
        state.record_tool_result("call_1", '{"content": "print(1)"}')

        messages = self.make_builder().build(state)

        self.assertEqual(
            messages,
            [
                {"role": "system", "content": "S"},
                {"role": "user", "content": "Read main.py"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "main.py"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": '{"content": "print(1)"}',
                },
            ],
        )

    def test_build_rejects_unresolved_tool_calls(self):
        state = AgentState()
        state.begin_turn("Read main.py")
        state.record_tool_calls([_tool_call("call_1")])

        with self.assertRaisesRegex(RuntimeError, "unresolved"):
            self.make_builder().build(state)

    def test_completed_tool_turn_is_retained_as_a_whole(self):
        state = AgentState()
        state.begin_turn("q1")
        state.record_tool_calls([_tool_call("call_1", arguments_json='{"path": "a"}')])
        state.record_tool_result("call_1", "R1")
        state.complete_turn("A1")
        state.begin_turn("current")

        messages = self.make_builder().build(state)

        self.assertEqual(
            messages,
            [
                {"role": "system", "content": "S"},
                {"role": "user", "content": "q1"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "a"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "R1"},
                {"role": "assistant", "content": "A1"},
                {"role": "user", "content": "current"},
            ],
        )

    def test_completed_tool_turn_is_removed_as_a_whole(self):
        state = AgentState()
        state.begin_turn("q1")
        state.record_tool_calls([_tool_call("call_1")])
        state.record_tool_result("call_1", "R1")
        state.complete_turn("A1")
        state.begin_turn("current")

        counter = LeafLengthTokenCounter()
        required = counter.count_message(
            {"role": "system", "content": "S"}
        ) + counter.count_message({"role": "user", "content": "current"})
        builder = ContextBuilder(
            system_prompt="S",
            max_input_tokens=required,
            token_counter=counter,
        )

        messages = builder.build(state)

        self.assertEqual(
            messages,
            [
                {"role": "system", "content": "S"},
                {"role": "user", "content": "current"},
            ],
        )

    def test_pending_tool_turn_is_retained_over_budget(self):
        state = AgentState()
        state.begin_turn("old")
        state.complete_turn("old answer")
        state.begin_turn("Read main.py")
        state.record_tool_calls([_tool_call("call_1")])
        state.record_tool_result("call_1", '{"content": "print(1)"}')

        messages = self.make_builder(max_input_tokens=1).build(state)

        self.assertEqual(
            messages,
            [
                {"role": "system", "content": "S"},
                {"role": "user", "content": "Read main.py"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "main.py"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": '{"content": "print(1)"}',
                },
            ],
        )

    def test_returned_tool_messages_are_defensive_copies(self):
        state = AgentState()
        state.begin_turn("Read main.py")
        state.record_tool_calls([_tool_call("call_1")])
        state.record_tool_result("call_1", "result")

        messages: list[Any] = self.make_builder().build(state)
        messages[2]["tool_calls"][0]["function"]["arguments"] = "hacked"

        self.assertEqual(
            state.pending_messages[1],
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "main.py"}',
                        },
                    }
                ],
            },
        )


class TiktokenTokenCounterTests(unittest.TestCase):
    def test_unknown_model_uses_fallback_encoding(self):
        encoding = Mock()
        encoding.encode.side_effect = lambda value: list(value)

        with (
            patch("tiktoken.encoding_for_model", side_effect=KeyError),
            patch("tiktoken.get_encoding", return_value=encoding) as get_encoding,
        ):
            counter = TiktokenTokenCounter("custom-model")

            self.assertEqual(
                counter.count_message({"role": "user", "content": "hi"}),
                10,
            )

        get_encoding.assert_called_once_with("o200k_base")

    def test_count_message_rejects_invalid_role_or_content(self):
        encoding = Mock()

        with patch("tiktoken.encoding_for_model", return_value=encoding):
            counter = TiktokenTokenCounter("test-model")

        invalid_messages = [
            {"content": "hello"},
            {"role": "user"},
            {"role": 1, "content": "hello"},
            {"role": "user", "content": None},
        ]
        for message in invalid_messages:
            with self.subTest(message=message), self.assertRaises(TypeError):
                counter.count_message(message)

    def test_count_message_includes_tool_call_arguments_and_results(self):
        encoding = Mock()
        encoding.encode.side_effect = lambda value: list(value)

        with patch("tiktoken.encoding_for_model", return_value=encoding):
            counter = TiktokenTokenCounter("test-model")

            tool_call_message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "rf", "arguments": "ARGS"},
                    }
                ],
            }
            # Leaves: assistant(9) + c1(2) + function(8) + rf(2) + ARGS(4) = 25, plus 4 overhead.
            self.assertEqual(counter.count_message(tool_call_message), 25 + 4)

            tool_result_message = {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "RESULT",
            }
            # Leaves: tool(4) + c1(2) + RESULT(6) = 12, plus 4 overhead.
            self.assertEqual(counter.count_message(tool_result_message), 12 + 4)


if __name__ == "__main__":
    unittest.main()
