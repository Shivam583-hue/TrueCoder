import unittest

from truecoder.agent import AgentState


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


if __name__ == "__main__":
    unittest.main()
