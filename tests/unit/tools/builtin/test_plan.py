import json
import unittest

from truecoder.planning import MAX_PLAN_STEPS, MAX_STEP_TITLE_LENGTH, PlanStore
from truecoder.tools.base import (
    ToolApproval,
    ToolArgumentError,
    ToolCall,
    ToolResultStatus,
)
from truecoder.tools.builtin.plan import UpdatePlanTool
from truecoder.tools.executor import ToolExecutor
from truecoder.tools.registry import ToolRegistry


def _arguments(*pairs: tuple[str, str]) -> str:
    return json.dumps(
        {"steps": [{"title": title, "status": status} for title, status in pairs]}
    )


class UpdatePlanToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = PlanStore()
        self.tool = UpdatePlanTool(self.store)

    async def _run(self, arguments_json: str):
        return await self.tool.run(self.tool.parse_arguments(arguments_json))

    def test_the_tool_does_not_require_approval(self):
        self.assertIs(self.tool.approval, ToolApproval.NOT_REQUIRED)

    def test_a_store_is_required(self):
        with self.assertRaises(TypeError):
            UpdatePlanTool(object())  # type: ignore[arg-type]

    async def test_running_the_tool_stores_the_plan(self):
        await self._run(
            _arguments(
                ("Read the failing test", "done"),
                ("Fix the parser", "in_progress"),
                ("Run the suite", "pending"),
            )
        )

        plan = self.store.current
        assert plan is not None
        self.assertEqual(
            [step.title for step in plan.steps],
            ["Read the failing test", "Fix the parser", "Run the suite"],
        )

    async def test_the_output_summarizes_progress(self):
        output = await self._run(
            _arguments(
                ("Read the failing test", "done"),
                ("Fix the parser", "in_progress"),
                ("Run the suite", "pending"),
            )
        )

        self.assertEqual(
            output,
            {"total": 3, "completed": 1, "in_progress": "Fix the parser"},
        )

    async def test_a_plan_without_an_active_step_reports_none(self):
        output = await self._run(_arguments(("Read the file", "pending")))

        self.assertIsNone(output["in_progress"])

    async def test_a_later_call_replaces_the_whole_plan(self):
        await self._run(_arguments(("First", "pending"), ("Second", "pending")))

        await self._run(_arguments(("Only", "done")))

        plan = self.store.current
        assert plan is not None
        self.assertEqual([step.title for step in plan.steps], ["Only"])

    async def test_titles_are_normalized(self):
        await self._run(_arguments(("  Fix   the parser ", "pending")))

        plan = self.store.current
        assert plan is not None
        self.assertEqual(plan.steps[0].title, "Fix the parser")

    async def test_two_steps_in_progress_report_a_recoverable_failure(self):
        registry = ToolRegistry()
        registry.register(self.tool)
        call = ToolCall(
            call_id="call_1",
            name="update_plan",
            arguments_json=_arguments(("A", "in_progress"), ("B", "in_progress")),
        )

        result = await ToolExecutor(registry).execute(call)

        self.assertIs(result.status, ToolResultStatus.ERROR)
        self.assertEqual(result.error_code, "invalid_plan")
        self.assertIsNone(self.store.current)

    async def test_a_whitespace_only_title_reports_a_recoverable_failure(self):
        registry = ToolRegistry()
        registry.register(self.tool)
        call = ToolCall(
            call_id="call_1",
            name="update_plan",
            arguments_json=_arguments(("   ", "pending")),
        )

        result = await ToolExecutor(registry).execute(call)

        self.assertIs(result.status, ToolResultStatus.ERROR)
        self.assertEqual(result.error_code, "invalid_plan")

    def test_an_unknown_status_is_rejected_during_parsing(self):
        with self.assertRaises(ToolArgumentError):
            self.tool.parse_arguments(_arguments(("Fix the parser", "blocked")))

    def test_an_empty_plan_is_rejected_during_parsing(self):
        with self.assertRaises(ToolArgumentError):
            self.tool.parse_arguments(json.dumps({"steps": []}))

    def test_more_steps_than_the_limit_are_rejected_during_parsing(self):
        pairs = tuple((f"Step {index}", "pending") for index in range(MAX_PLAN_STEPS + 1))

        with self.assertRaises(ToolArgumentError):
            self.tool.parse_arguments(_arguments(*pairs))

    def test_a_title_longer_than_the_limit_is_rejected_during_parsing(self):
        title = "a" * (MAX_STEP_TITLE_LENGTH + 1)

        with self.assertRaises(ToolArgumentError):
            self.tool.parse_arguments(_arguments((title, "pending")))

    def test_unknown_step_fields_are_rejected_during_parsing(self):
        arguments = json.dumps(
            {"steps": [{"title": "Fix it", "status": "pending", "owner": "me"}]}
        )

        with self.assertRaises(ToolArgumentError):
            self.tool.parse_arguments(arguments)


class UpdatePlanSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = UpdatePlanTool(PlanStore()).definition().parameters

    def test_every_object_in_the_schema_forbids_extra_properties(self):
        objects = [self.schema, *self.schema["$defs"].values()]

        for candidate in objects:
            with self.subTest(title=candidate.get("title")):
                self.assertIs(candidate["additionalProperties"], False)

    def test_the_nested_step_lists_every_field_as_required(self):
        step_schema = self.schema["$defs"]["PlanStepArgument"]

        self.assertEqual(
            sorted(step_schema["required"]),
            sorted(step_schema["properties"]),
        )

    def test_the_status_field_is_an_enum_of_the_supported_statuses(self):
        step_schema = self.schema["$defs"]["PlanStepArgument"]

        self.assertEqual(
            step_schema["properties"]["status"]["enum"],
            ["pending", "in_progress", "done"],
        )
