from __future__ import annotations

import unittest

from truecoder.planning import Plan, PlanStep, PlanStepStatus
from truecoder.tui.widgets import PLAN_STEP_GLYPHS, PlanCard, StatusBar


def _plan(*pairs: tuple[str, PlanStepStatus]) -> Plan:
    return Plan(tuple(PlanStep(title=title, status=status) for title, status in pairs))


class PlanCardConstructionTests(unittest.TestCase):
    def test_requires_a_plan(self):
        with self.assertRaises(TypeError):
            PlanCard("Fix the parser")  # type: ignore[arg-type]

    def test_an_incomplete_plan_is_not_marked_complete(self):
        card = PlanCard(_plan(("Fix the parser", "in_progress")))

        self.assertNotIn("complete", card.classes)

    def test_a_finished_plan_is_marked_complete_from_the_start(self):
        card = PlanCard(_plan(("Fix the parser", "done")))

        self.assertIn("complete", card.classes)


class PlanCardRenderingTests(unittest.TestCase):
    def test_progress_counts_completed_steps(self):
        card = PlanCard(
            _plan(
                ("First", "done"),
                ("Second", "in_progress"),
                ("Third", "pending"),
            )
        )

        self.assertEqual(card._progress_label(), "1/3")

    def test_every_step_appears_with_its_glyph(self):
        card = PlanCard(
            _plan(
                ("Read the failing test", "done"),
                ("Fix the parser", "in_progress"),
                ("Run the suite", "pending"),
            )
        )

        lines = card._steps_text().plain.splitlines()

        self.assertEqual(
            lines,
            [
                f"{PLAN_STEP_GLYPHS['done']} Read the failing test",
                f"{PLAN_STEP_GLYPHS['in_progress']} Fix the parser",
                f"{PLAN_STEP_GLYPHS['pending']} Run the suite",
            ],
        )

    def test_each_status_is_styled_distinctly(self):
        card = PlanCard(
            _plan(("First", "done"), ("Second", "in_progress"), ("Third", "pending"))
        )

        styles = {str(span.style) for span in card._steps_text().spans}

        self.assertEqual(len(styles), 3)


class PlanCardUpdateTests(unittest.TestCase):
    def test_updating_replaces_the_held_plan(self):
        card = PlanCard(_plan(("First", "pending")))

        card.update_plan(_plan(("Second", "done"), ("Third", "in_progress")))

        self.assertEqual([step.title for step in card.plan.steps], ["Second", "Third"])
        self.assertEqual(card._progress_label(), "1/2")

    def test_updating_to_a_finished_plan_marks_it_complete(self):
        card = PlanCard(_plan(("First", "in_progress")))

        card.update_plan(_plan(("First", "done")))

        self.assertIn("complete", card.classes)

    def test_updating_away_from_a_finished_plan_clears_the_marker(self):
        card = PlanCard(_plan(("First", "done")))

        card.update_plan(_plan(("First", "done"), ("Second", "in_progress")))

        self.assertNotIn("complete", card.classes)

    def test_updating_requires_a_plan(self):
        card = PlanCard(_plan(("First", "pending")))

        with self.assertRaises(TypeError):
            card.update_plan(None)  # type: ignore[arg-type]


class StatusBarPlanTests(unittest.TestCase):
    def _bar(self) -> StatusBar:
        bar = StatusBar("/workspace", max_input_tokens=1000)
        bar.set_conversation_active(True)
        return bar

    def test_no_plan_label_without_a_plan(self):
        self.assertNotIn("plan", self._bar()._right_label().plain)

    def test_the_plan_label_shows_progress(self):
        bar = self._bar()

        bar.set_plan(_plan(("First", "done"), ("Second", "in_progress")))

        self.assertIn("plan 1/2", bar._right_label().plain)

    def test_the_plan_label_is_hidden_before_the_conversation_starts(self):
        bar = StatusBar("/workspace")

        bar.set_plan(_plan(("First", "done")))

        self.assertNotIn("plan", bar._right_label().plain)

    def test_reset_clears_the_plan(self):
        bar = self._bar()
        bar.set_plan(_plan(("First", "done")))

        bar.reset()
        bar.set_conversation_active(True)

        self.assertNotIn("plan", bar._right_label().plain)

    def test_clearing_the_plan_removes_the_label(self):
        bar = self._bar()
        bar.set_plan(_plan(("First", "done")))

        bar.set_plan(None)

        self.assertNotIn("plan", bar._right_label().plain)

    def test_a_non_plan_is_rejected(self):
        with self.assertRaises(TypeError):
            self._bar().set_plan("1/2")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
