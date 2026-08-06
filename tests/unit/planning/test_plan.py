import unittest

from truecoder.planning import (
    MAX_PLAN_STEPS,
    MAX_STEP_TITLE_LENGTH,
    Plan,
    PlanStep,
    PlanStepStatus,
    PlanStore,
    normalize_step_title,
)


class RecordingSink:
    def __init__(self) -> None:
        self.published: list[Plan | None] = []

    def publish(self, plan: Plan | None) -> None:
        self.published.append(plan)


def _steps(*pairs: tuple[str, PlanStepStatus]) -> tuple[PlanStep, ...]:
    return tuple(PlanStep(title=title, status=status) for title, status in pairs)


def _pending_steps(count: int) -> tuple[PlanStep, ...]:
    return tuple(
        PlanStep(title=f"Step {index}", status="pending") for index in range(count)
    )


class NormalizeStepTitleTests(unittest.TestCase):
    def test_surrounding_whitespace_is_removed(self):
        self.assertEqual(normalize_step_title("  Read the file  "), "Read the file")

    def test_internal_whitespace_runs_collapse_to_single_spaces(self):
        self.assertEqual(
            normalize_step_title("Read\n  the\tfile"),
            "Read the file",
        )

    def test_a_blank_title_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_step_title("   \n  ")

    def test_a_title_longer_than_the_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_step_title("a" * (MAX_STEP_TITLE_LENGTH + 1))

    def test_a_title_at_the_limit_is_accepted(self):
        title = "a" * MAX_STEP_TITLE_LENGTH

        self.assertEqual(normalize_step_title(title), title)

    def test_a_non_string_title_is_rejected(self):
        with self.assertRaises(TypeError):
            normalize_step_title(None)  # type: ignore[arg-type]


class PlanStepTests(unittest.TestCase):
    def test_the_title_is_normalized_on_construction(self):
        step = PlanStep(title="  Fix   the parser ", status="pending")

        self.assertEqual(step.title, "Fix the parser")

    def test_an_unknown_status_is_rejected(self):
        with self.assertRaises(ValueError):
            PlanStep(title="Fix the parser", status="blocked")  # type: ignore[arg-type]


class PlanTests(unittest.TestCase):
    def test_an_empty_plan_is_rejected(self):
        with self.assertRaises(ValueError):
            Plan(())

    def test_more_steps_than_the_limit_are_rejected(self):
        with self.assertRaises(ValueError):
            Plan(_pending_steps(MAX_PLAN_STEPS + 1))

    def test_a_plan_at_the_step_limit_is_accepted(self):
        plan = Plan(_pending_steps(MAX_PLAN_STEPS))

        self.assertEqual(plan.total, MAX_PLAN_STEPS)

    def test_two_steps_in_progress_are_rejected(self):
        steps = _steps(("First", "in_progress"), ("Second", "in_progress"))

        with self.assertRaises(ValueError):
            Plan(steps)

    def test_one_step_in_progress_is_accepted(self):
        plan = Plan(_steps(("First", "done"), ("Second", "in_progress")))

        self.assertIsNotNone(plan.active_step)
        assert plan.active_step is not None
        self.assertEqual(plan.active_step.title, "Second")

    def test_a_plan_without_an_active_step_reports_none(self):
        plan = Plan(_steps(("First", "done"), ("Second", "pending")))

        self.assertIsNone(plan.active_step)

    def test_progress_counts_only_completed_steps(self):
        plan = Plan(
            _steps(
                ("First", "done"),
                ("Second", "in_progress"),
                ("Third", "pending"),
            )
        )

        self.assertEqual(plan.completed, 1)
        self.assertEqual(plan.total, 3)
        self.assertFalse(plan.is_complete)

    def test_a_fully_completed_plan_reports_complete(self):
        plan = Plan(_steps(("First", "done"), ("Second", "done")))

        self.assertTrue(plan.is_complete)

    def test_non_step_values_are_rejected(self):
        with self.assertRaises(TypeError):
            Plan(("Read the file",))  # type: ignore[arg-type]

    def test_render_numbers_steps_and_marks_status(self):
        plan = Plan(
            _steps(
                ("Read the failing test", "done"),
                ("Fix the parser", "in_progress"),
                ("Run the suite", "pending"),
            )
        )

        rendered = plan.render()

        self.assertIn("1. [x] Read the failing test", rendered)
        self.assertIn("2. [>] Fix the parser", rendered)
        self.assertIn("3. [ ] Run the suite", rendered)


class PlanStoreTests(unittest.TestCase):
    def test_a_new_store_holds_no_plan(self):
        store = PlanStore()

        self.assertIsNone(store.current)

    def test_replace_stores_and_returns_the_plan(self):
        store = PlanStore()

        plan = store.replace(_steps(("Read the file", "in_progress")))

        self.assertIs(store.current, plan)
        self.assertEqual(plan.total, 1)

    def test_replace_swaps_the_whole_plan(self):
        store = PlanStore()
        store.replace(_steps(("First", "pending"), ("Second", "pending")))

        store.replace(_steps(("Only", "done")))

        assert store.current is not None
        self.assertEqual([step.title for step in store.current.steps], ["Only"])

    def test_an_invalid_replacement_leaves_the_existing_plan_intact(self):
        store = PlanStore()
        original = store.replace(_steps(("First", "pending")))

        with self.assertRaises(ValueError):
            store.replace(_steps(("A", "in_progress"), ("B", "in_progress")))

        self.assertIs(store.current, original)

    def test_clear_removes_the_plan(self):
        store = PlanStore()
        store.replace(_steps(("First", "pending")))

        store.clear()

        self.assertIsNone(store.current)

    def test_clear_without_a_plan_does_not_publish(self):
        sink = RecordingSink()
        store = PlanStore(sink)

        store.clear()

        self.assertEqual(sink.published, [])

    def test_the_sink_receives_every_change(self):
        sink = RecordingSink()
        store = PlanStore(sink)

        plan = store.replace(_steps(("First", "pending")))
        store.clear()

        self.assertEqual(sink.published, [plan, None])

    def test_a_sink_can_be_attached_after_construction(self):
        sink = RecordingSink()
        store = PlanStore()
        store.attach_sink(sink)

        plan = store.replace(_steps(("First", "pending")))

        self.assertEqual(sink.published, [plan])

    def test_a_sink_without_publish_is_rejected(self):
        with self.assertRaises(TypeError):
            PlanStore(object())  # type: ignore[arg-type]
