from __future__ import annotations

import unittest

from truecoder.execution.models import EXECUTION_LIFECYCLE_STAGES
from truecoder.tui.execution_view import TRUNCATION_NOTE
from truecoder.tui.widgets import ExecutionCard


class ExecutionCardConstructionTests(unittest.TestCase):
    def test_requires_a_non_empty_execution_id(self):
        with self.assertRaises(ValueError):
            ExecutionCard("", "pytest")
        with self.assertRaises(ValueError):
            ExecutionCard("   ", "pytest")

    def test_starts_in_a_preparing_state_before_any_event(self):
        card = ExecutionCard("exec-1", "pytest -q")

        self.assertEqual(card.state, "preparing")
        self.assertIsNone(card.stage)
        self.assertIsNone(card.audit_id)
        self.assertFalse(card.cancel_requested)

    def test_an_empty_command_still_renders_something(self):
        card = ExecutionCard("exec-1", "")

        self.assertEqual(card.command, "(no command)")


class ExecutionCardStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.card = ExecutionCard("exec-1", "pytest -q")

    def test_every_lifecycle_stage_is_accepted(self):
        for stage in EXECUTION_LIFECYCLE_STAGES:
            ExecutionCard("exec-1", "cmd").apply_stage(stage)

    def test_unknown_stage_is_refused_rather_than_rendered(self):
        with self.assertRaises(ValueError):
            self.card.apply_stage("invented")

    def test_stages_advance_the_visible_state(self):
        self.card.apply_stage("starting")
        self.assertEqual(self.card.state, "starting")

        self.card.apply_stage("started")
        self.assertEqual(self.card.state, "running")

        self.card.apply_stage("completed")
        self.assertEqual(self.card.state, "completed")
        self.assertEqual(self.card.stage, "completed")

    def test_a_denial_is_distinguished_from_a_failure(self):
        self.card.apply_stage("denied")
        self.assertEqual(self.card.state, "rejected")

    def test_a_timeout_is_distinguished_from_a_cancellation(self):
        timed_out = ExecutionCard("exec-1", "cmd")
        timed_out.apply_stage("timed_out")
        cancelled = ExecutionCard("exec-2", "cmd")
        cancelled.apply_stage("cancelled")

        self.assertEqual(timed_out.state, "failed")
        self.assertEqual(cancelled.state, "cancelled")


class ExecutionCardCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.card = ExecutionCard("exec-1", "sleep 100")
        self.card.apply_stage("started")

    def test_marking_cancelling_is_not_a_terminal_state(self):
        self.card.mark_cancelling()

        self.assertEqual(self.card.state, "cancelling")
        self.assertTrue(self.card.cancel_requested)
        self.assertNotEqual(self.card.stage, "cancelled")

    def test_a_second_cancel_request_is_idempotent(self):
        self.card.mark_cancelling()
        self.card.mark_cancelling()

        self.assertEqual(self.card.state, "cancelling")
        self.assertTrue(self.card.cancel_requested)

    def test_a_natural_exit_after_cancelling_still_wins_the_display(self):
        self.card.mark_cancelling()
        self.card.apply_stage("completed")

        self.assertEqual(self.card.state, "completed")

    def test_cancel_message_carries_the_execution_id(self):
        message = ExecutionCard.CancelRequested("exec-42")

        self.assertEqual(message.execution_id, "exec-42")


class ExecutionCardOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.card = ExecutionCard("exec-1", "pytest -q")

    def test_output_preview_stays_bounded(self):
        for index in range(5000):
            self.card.append_output("stdout", f"line {index}\n")

        text = self.card._preview.text()
        self.assertIn(TRUNCATION_NOTE, text)
        self.assertLessEqual(len(text.splitlines()), 201)

    def test_both_streams_feed_the_same_ordered_preview(self):
        self.card.append_output("stdout", "out\n")
        self.card.append_output("stderr", "err\n")

        self.assertEqual(self.card._preview.text(), "out\nerr")


class ExecutionCardResultTests(unittest.TestCase):
    def test_finishing_records_the_audit_identity(self):
        card = ExecutionCard("exec-1", "pytest -q")
        card.apply_stage("completed")
        card.finish("exit 0 · 1.2s", audit_id="audit-7")

        self.assertEqual(card.audit_id, "audit-7")
        self.assertIn("audit-7", card._details_text())

    def test_headline_includes_the_result_summary(self):
        card = ExecutionCard("exec-1", "pytest -q")
        card.finish("exit 1")

        self.assertIn("pytest -q", card._headline())
        self.assertIn("exit 1", card._headline())

    def test_compact_rows_replace_the_bare_command_summary(self):
        card = ExecutionCard("exec-1", "pytest -q")
        self.assertEqual(card._compact_text(), "pytest -q")

        card.set_approval((("Backend", "container"),), (("Risk", "medium"),))

        self.assertEqual(card._compact_text(), "Backend: container")
        self.assertIn("Risk: medium", card._details_text())


if __name__ == "__main__":
    unittest.main()
