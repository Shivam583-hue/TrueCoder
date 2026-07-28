import unittest
from typing import Any

from truecoder.agent import AgentState
from truecoder.tools import ToolCall


def _call(call_id: str, name: str = "read_file", arguments_json: str = "{}") -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments_json=arguments_json)


class AgentStateTests(unittest.TestCase):
    def test_new_state_is_empty_and_inactive(self):
        state = AgentState()

        self.assertEqual(state.messages, [])
        self.assertIsNone(state.pending_prompt)
        self.assertFalse(state.turn_active)
        self.assertEqual(state.messages_for_context(), [])

    def test_begin_turn_normalizes_and_exposes_pending_prompt(self):
        state = AgentState()

        state.begin_turn("  Explain this code  ")

        self.assertTrue(state.turn_active)
        self.assertEqual(state.pending_prompt, "Explain this code")
        self.assertEqual(state.messages, [])
        self.assertEqual(
            state.messages_for_context(),
            [{"role": "user", "content": "Explain this code"}],
        )

    def test_complete_turn_records_a_user_assistant_pair(self):
        state = AgentState()
        state.begin_turn("Question")

        state.complete_turn("Answer")

        self.assertEqual(
            state.messages,
            [
                {"role": "user", "content": "Question"},
                {"role": "assistant", "content": "Answer"},
            ],
        )
        self.assertFalse(state.turn_active)
        self.assertIsNone(state.pending_prompt)

    def test_begin_turn_rejects_empty_prompt(self):
        state = AgentState()

        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            state.begin_turn("   ")

    def test_begin_turn_rejects_overlapping_turn(self):
        state = AgentState()
        state.begin_turn("First")

        with self.assertRaisesRegex(RuntimeError, "already active"):
            state.begin_turn("Second")

        self.assertEqual(state.pending_prompt, "First")

    def test_complete_turn_requires_an_active_turn(self):
        state = AgentState()

        with self.assertRaisesRegex(RuntimeError, "no active turn"):
            state.complete_turn("Answer")

    def test_abort_turn_is_idempotent_and_discards_pending_prompt(self):
        state = AgentState()
        state.begin_turn("Discard me")

        state.abort_turn()
        state.abort_turn()

        self.assertFalse(state.turn_active)
        self.assertEqual(state.messages, [])

    def test_reset_clears_completed_and_pending_state(self):
        state = AgentState()
        state.begin_turn("Completed question")
        state.complete_turn("Completed answer")
        state.begin_turn("Pending question")

        state.reset()

        self.assertEqual(state.messages, [])
        self.assertIsNone(state.pending_prompt)
        self.assertFalse(state.turn_active)

    def test_returned_messages_are_defensive_copies(self):
        state = AgentState()
        state.begin_turn("Question")
        state.complete_turn("Answer")

        messages = state.messages
        context_messages = state.messages_for_context()
        messages[0]["content"] = "changed"
        context_messages[1]["content"] = "also changed"
        messages.append({"role": "user", "content": "injected"})

        self.assertEqual(
            state.messages,
            [
                {"role": "user", "content": "Question"},
                {"role": "assistant", "content": "Answer"},
            ],
        )

    def test_replaces_history_with_validated_completed_turns(self):
        state = AgentState()
        state.replace_completed_turns(
            [
                [
                    {"role": "user", "content": "Question"},
                    {"role": "assistant", "content": "Answer"},
                ]
            ]
        )

        self.assertEqual(
            state.completed_turns,
            [
                [
                    {"role": "user", "content": "Question"},
                    {"role": "assistant", "content": "Answer"},
                ]
            ],
        )

    def test_failed_history_replacement_is_atomic(self):
        state = AgentState()
        state.begin_turn("Existing")
        state.complete_turn("History")

        with self.assertRaises(ValueError):
            state.replace_completed_turns(
                [[{"role": "user", "content": "Incomplete"}]]
            )

        self.assertEqual(
            state.messages,
            [
                {"role": "user", "content": "Existing"},
                {"role": "assistant", "content": "History"},
            ],
        )

    def test_rejects_history_replacement_during_active_turn(self):
        state = AgentState()
        state.begin_turn("Pending")

        with self.assertRaises(RuntimeError):
            state.replace_completed_turns([])


class AgentStateToolTurnTests(unittest.TestCase):
    def test_single_tool_call_round_produces_provider_ordered_history(self):
        state = AgentState()
        state.begin_turn("Read main.py")

        state.record_tool_calls(
            [_call("call_1", arguments_json='{"path": "main.py"}')],
        )
        self.assertEqual(state.outstanding_tool_call_ids, ("call_1",))

        state.record_tool_result("call_1", '{"content": "print(1)"}')
        self.assertEqual(state.outstanding_tool_call_ids, ())

        state.complete_turn("Here is the file.")

        expected = [
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
            {"role": "assistant", "content": "Here is the file."},
        ]
        self.assertEqual(state.messages, expected)
        self.assertEqual(state.completed_turns, [expected])
        self.assertFalse(state.turn_active)

    def test_assistant_tool_call_content_is_preserved(self):
        state = AgentState()
        state.begin_turn("Read main.py")

        state.record_tool_calls([_call("call_1")], content="Let me read that.")

        assistant_message = state.pending_messages[1]
        self.assertEqual(assistant_message["content"], "Let me read that.")

    def test_multiple_tool_calls_in_a_batch_preserve_order(self):
        state = AgentState()
        state.begin_turn("Inspect the project")

        state.record_tool_calls([_call("call_1"), _call("call_2")])
        self.assertEqual(state.outstanding_tool_call_ids, ("call_1", "call_2"))

        state.record_tool_result("call_1", "first")
        self.assertEqual(state.outstanding_tool_call_ids, ("call_2",))
        state.record_tool_result("call_2", "second")
        self.assertEqual(state.outstanding_tool_call_ids, ())

        state.complete_turn("Done")

        roles = [message["role"] for message in state.messages]
        self.assertEqual(
            roles,
            ["user", "assistant", "tool", "tool", "assistant"],
        )

    def test_multiple_tool_rounds_before_final_text(self):
        state = AgentState()
        state.begin_turn("Explore then summarize")

        state.record_tool_calls([_call("call_1")])
        state.record_tool_result("call_1", "first")
        state.record_tool_calls([_call("call_2")])
        state.record_tool_result("call_2", "second")
        state.complete_turn("Summary")

        roles = [message["role"] for message in state.messages]
        self.assertEqual(
            roles,
            ["user", "assistant", "tool", "assistant", "tool", "assistant"],
        )

    def test_pending_prompt_survives_recorded_tool_calls(self):
        state = AgentState()
        state.begin_turn("Read main.py")

        state.record_tool_calls([_call("call_1")])

        self.assertEqual(state.pending_prompt, "Read main.py")

    def test_messages_for_context_includes_pending_tool_messages(self):
        state = AgentState()
        state.begin_turn("Read main.py")
        state.record_tool_calls([_call("call_1")])
        state.record_tool_result("call_1", "result")

        roles = [message["role"] for message in state.messages_for_context()]
        self.assertEqual(roles, ["user", "assistant", "tool"])

    def test_record_tool_calls_requires_an_active_turn(self):
        state = AgentState()

        with self.assertRaisesRegex(RuntimeError, "no active turn to record tool calls"):
            state.record_tool_calls([_call("call_1")])

    def test_record_tool_calls_rejects_an_empty_batch(self):
        state = AgentState()
        state.begin_turn("Question")

        with self.assertRaisesRegex(ValueError, "At least one tool call"):
            state.record_tool_calls([])

    def test_record_tool_calls_rejects_duplicate_ids_within_a_batch(self):
        state = AgentState()
        state.begin_turn("Question")

        with self.assertRaisesRegex(ValueError, "unique"):
            state.record_tool_calls([_call("call_1"), _call("call_1")])

    def test_record_tool_calls_rejects_reused_ids_across_batches(self):
        state = AgentState()
        state.begin_turn("Question")
        state.record_tool_calls([_call("call_1")])
        state.record_tool_result("call_1", "result")

        with self.assertRaisesRegex(ValueError, "reused"):
            state.record_tool_calls([_call("call_1")])

    def test_record_tool_calls_rejects_overlapping_unresolved_calls(self):
        state = AgentState()
        state.begin_turn("Question")
        state.record_tool_calls([_call("call_1")])

        with self.assertRaisesRegex(RuntimeError, "Earlier tool calls remain unresolved"):
            state.record_tool_calls([_call("call_2")])

    def test_record_tool_result_requires_an_active_turn(self):
        state = AgentState()

        with self.assertRaisesRegex(
            RuntimeError, "no active turn to record a tool result"
        ):
            state.record_tool_result("call_1", "result")

    def test_record_tool_result_requires_outstanding_calls(self):
        state = AgentState()
        state.begin_turn("Question")

        with self.assertRaisesRegex(RuntimeError, "no outstanding tool calls"):
            state.record_tool_result("call_1", "result")

    def test_record_tool_result_rejects_unknown_id(self):
        state = AgentState()
        state.begin_turn("Question")
        state.record_tool_calls([_call("call_1")])

        with self.assertRaisesRegex(ValueError, "resolved in order"):
            state.record_tool_result("call_unknown", "result")

    def test_record_tool_result_rejects_out_of_order_id(self):
        state = AgentState()
        state.begin_turn("Question")
        state.record_tool_calls([_call("call_1"), _call("call_2")])

        with self.assertRaisesRegex(ValueError, "resolved in order"):
            state.record_tool_result("call_2", "result")

    def test_record_tool_result_rejects_empty_content(self):
        state = AgentState()
        state.begin_turn("Question")
        state.record_tool_calls([_call("call_1")])

        with self.assertRaisesRegex(ValueError, "content cannot be empty"):
            state.record_tool_result("call_1", "")

    def test_complete_turn_rejects_unresolved_tool_calls(self):
        state = AgentState()
        state.begin_turn("Question")
        state.record_tool_calls([_call("call_1")])

        with self.assertRaisesRegex(RuntimeError, "remain unresolved"):
            state.complete_turn("Answer")

    def test_abort_turn_discards_pending_tool_state(self):
        state = AgentState()
        state.begin_turn("Question")
        state.record_tool_calls([_call("call_1")])

        state.abort_turn()

        self.assertFalse(state.turn_active)
        self.assertEqual(state.outstanding_tool_call_ids, ())
        # A previously seen id can be reused once the turn is discarded.
        state.begin_turn("Question again")
        state.record_tool_calls([_call("call_1")])
        self.assertEqual(state.outstanding_tool_call_ids, ("call_1",))

    def test_reset_discards_pending_tool_state(self):
        state = AgentState()
        state.begin_turn("Question")
        state.record_tool_calls([_call("call_1")])

        state.reset()

        self.assertFalse(state.turn_active)
        self.assertEqual(state.outstanding_tool_call_ids, ())
        self.assertEqual(state.messages, [])

    def test_returned_tool_messages_cannot_mutate_state(self):
        state = AgentState()
        state.begin_turn("Read main.py")
        state.record_tool_calls([_call("call_1", arguments_json='{"path": "a"}')])
        state.record_tool_result("call_1", "result")
        state.complete_turn("Done")

        messages: list[Any] = state.messages
        turns: list[Any] = state.completed_turns
        messages[1]["tool_calls"][0]["function"]["arguments"] = "hacked"
        turns[0][1]["tool_calls"][0]["function"]["name"] = "hacked"

        self.assertEqual(
            state.messages[1],
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
        )

    def test_outstanding_tool_call_ids_are_immutable(self):
        state = AgentState()
        state.begin_turn("Question")
        state.record_tool_calls([_call("call_1")])

        self.assertIsInstance(state.outstanding_tool_call_ids, tuple)


if __name__ == "__main__":
    unittest.main()
