import asyncio
import unittest

from truecoder.tools import (
    BaseTool,
    ToolApproval,
    ToolArguments,
    ToolCall,
    ToolExecutionError,
    ToolExecutor,
    ToolRegistry,
    ToolResultStatus,
)


class NumberArguments(ToolArguments):
    value: int


class RecordingTool(BaseTool[NumberArguments]):
    name = "double"
    description = "Double an integer."
    arguments_type = NumberArguments
    approval = ToolApproval.NOT_REQUIRED

    def __init__(self) -> None:
        self.calls: list[NumberArguments] = []

    async def run(self, arguments: NumberArguments) -> int:
        self.calls.append(arguments)
        return arguments.value * 2


class ApprovalTool(RecordingTool):
    name = "approved_double"
    approval = ToolApproval.REQUIRED


class FailingTool(RecordingTool):
    name = "fail"

    async def run(self, arguments: NumberArguments) -> int:
        raise RuntimeError(f"Cannot process {arguments.value}")


class MissingFileTool(RecordingTool):
    name = "missing_file"

    async def run(self, arguments: NumberArguments) -> int:
        raise ToolExecutionError(
            "The requested file does not exist.",
            code="file_not_found",
        )


class CancellingTool(RecordingTool):
    name = "cancel"

    async def run(self, arguments: NumberArguments) -> int:
        raise asyncio.CancelledError


class ToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.executor = ToolExecutor(self.registry)

    async def test_executes_safe_tool_with_validated_arguments(self):
        tool = RecordingTool()
        self.registry.register(tool)

        result = await self.executor.execute(
            ToolCall("call_1", "double", '{"value": 4}'),
        )

        self.assertEqual(result.status, ToolResultStatus.SUCCESS)
        self.assertEqual(result.output, 8)
        self.assertEqual(tool.calls, [NumberArguments(value=4)])

    async def test_unknown_tool_returns_structured_error(self):
        result = await self.executor.execute(
            ToolCall("call_1", "missing", "{}"),
        )

        self.assertEqual(result.status, ToolResultStatus.ERROR)
        self.assertEqual(result.error_code, "tool_not_found")
        self.assertIn("missing", result.error)

    async def test_invalid_arguments_do_not_execute_the_tool(self):
        tool = RecordingTool()
        self.registry.register(tool)

        result = await self.executor.execute(
            ToolCall("call_1", "double", '{"value": "wrong"}'),
        )

        self.assertEqual(result.status, ToolResultStatus.ERROR)
        self.assertEqual(result.error_code, "invalid_arguments")
        self.assertEqual(tool.calls, [])

    async def test_arguments_are_validated_before_approval_is_requested(self):
        tool = ApprovalTool()
        self.registry.register(tool)

        result = await self.executor.execute(
            ToolCall("call_1", "approved_double", "{}"),
        )

        self.assertEqual(result.status, ToolResultStatus.ERROR)
        self.assertEqual(result.error_code, "invalid_arguments")
        self.assertEqual(tool.calls, [])

    async def test_required_tool_waits_for_explicit_approval(self):
        tool = ApprovalTool()
        self.registry.register(tool)
        call = ToolCall("call_1", "approved_double", '{"value": 4}')

        pending_result = await self.executor.execute(call)
        approved_result = await self.executor.execute(call, approved=True)

        self.assertEqual(
            pending_result.status,
            ToolResultStatus.APPROVAL_REQUIRED,
        )
        self.assertEqual(approved_result.status, ToolResultStatus.SUCCESS)
        self.assertEqual(approved_result.output, 8)
        self.assertEqual(tool.calls, [NumberArguments(value=4)])

    async def test_truthy_non_boolean_value_does_not_count_as_approval(self):
        tool = ApprovalTool()
        self.registry.register(tool)

        result = await self.executor.execute(
            ToolCall("call_1", "approved_double", '{"value": 4}'),
            approved="yes",  # type: ignore[arg-type]
        )

        self.assertEqual(result.status, ToolResultStatus.APPROVAL_REQUIRED)
        self.assertEqual(tool.calls, [])

    async def test_execution_failure_is_returned_as_structured_error(self):
        self.registry.register(FailingTool())

        result = await self.executor.execute(
            ToolCall("call_1", "fail", '{"value": 4}'),
        )

        self.assertEqual(result.status, ToolResultStatus.ERROR)
        self.assertEqual(result.error_code, "execution_failed")
        self.assertEqual(result.error, "Cannot process 4")

    async def test_domain_execution_failure_preserves_message_and_code(self):
        self.registry.register(MissingFileTool())

        result = await self.executor.execute(
            ToolCall("call_1", "missing_file", '{"value": 4}'),
        )

        self.assertEqual(result.status, ToolResultStatus.ERROR)
        self.assertEqual(result.error_code, "file_not_found")
        self.assertEqual(result.error, "The requested file does not exist.")

    async def test_cancellation_is_not_converted_to_a_tool_error(self):
        self.registry.register(CancellingTool())

        with self.assertRaises(asyncio.CancelledError):
            await self.executor.execute(
                ToolCall("call_1", "cancel", '{"value": 4}'),
            )


if __name__ == "__main__":
    unittest.main()
