import unittest
from typing import Any

from pydantic import ValidationError

from truecoder.tools import (
    BaseTool,
    ToolApproval,
    ToolArgumentError,
    ToolArguments,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
)


class AddArguments(ToolArguments):
    left: int
    right: int


class AddTool(BaseTool[AddArguments]):
    name = "add"
    description = "Add two integers."
    arguments_type = AddArguments
    approval = ToolApproval.NOT_REQUIRED

    async def run(self, arguments: AddArguments) -> int:
        return arguments.left + arguments.right


class InvalidArgumentsTypeTool(AddTool):
    arguments_type = dict  # type: ignore[assignment]


class InvalidApprovalTool(AddTool):
    approval = "not_required"  # type: ignore[assignment]


class ToolDefinitionTests(unittest.TestCase):
    def test_builds_chat_completion_schema_without_sharing_mutable_data(self):
        parameters = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        }
        definition = ToolDefinition(
            name=" search ",
            description=" Search documents. ",
            parameters=parameters,
        )

        parameters["properties"]["query"]["type"] = "integer"
        schema = definition.to_chat_completion_schema()
        schema["function"]["parameters"]["properties"]["query"]["type"] = "boolean"

        self.assertEqual(definition.name, "search")
        self.assertEqual(definition.description, "Search documents.")
        self.assertEqual(
            definition.parameters["properties"]["query"]["type"],
            "string",
        )
        self.assertEqual(
            definition.to_chat_completion_schema(),
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search documents.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            },
        )

    def test_rejects_invalid_metadata(self):
        valid_parameters = {"type": "object"}
        invalid_definitions: list[dict[str, Any]] = [
            {
                "name": "",
                "description": "Description",
                "parameters": valid_parameters,
            },
            {
                "name": "not valid",
                "description": "Description",
                "parameters": valid_parameters,
            },
            {
                "name": "valid",
                "description": " ",
                "parameters": valid_parameters,
            },
            {
                "name": "valid",
                "description": "Description",
                "parameters": {"type": "array"},
            },
        ]

        for values in invalid_definitions:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    ToolDefinition(**values)

        with self.assertRaises(TypeError):
            ToolDefinition(
                name="valid",
                description="Description",
                parameters=[],  # type: ignore[arg-type]
            )


class BaseToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = AddTool()

    def test_definition_uses_the_argument_model_schema(self):
        schema = self.tool.definition().to_chat_completion_schema()

        self.assertEqual(schema["function"]["name"], "add")
        self.assertEqual(schema["function"]["description"], "Add two integers.")
        self.assertTrue(schema["function"]["strict"])
        self.assertEqual(schema["function"]["parameters"]["type"], "object")
        self.assertFalse(
            schema["function"]["parameters"]["additionalProperties"],
        )
        self.assertEqual(
            schema["function"]["parameters"]["required"],
            ["left", "right"],
        )

    def test_parse_arguments_returns_a_validated_model(self):
        arguments = self.tool.parse_arguments('{"left": 2, "right": 3}')

        self.assertEqual(arguments, AddArguments(left=2, right=3))

    def test_parse_arguments_rejects_malformed_non_object_and_invalid_data(self):
        invalid_arguments = [
            '{"left":',
            "[2, 3]",
            '{"left": 2}',
            '{"left": 2, "right": 3, "extra": true}',
            '{"left": "wrong", "right": 3}',
        ]

        for arguments_json in invalid_arguments:
            with self.subTest(arguments_json=arguments_json):
                with self.assertRaises(ToolArgumentError):
                    self.tool.parse_arguments(arguments_json)

    def test_argument_models_forbid_extra_fields_directly(self):
        with self.assertRaises(ValidationError):
            AddArguments(left=1, right=2, extra=True)

    def test_definition_rejects_invalid_tool_class_metadata(self):
        with self.assertRaisesRegex(TypeError, "arguments_type"):
            InvalidArgumentsTypeTool().definition()

        with self.assertRaisesRegex(TypeError, "approval"):
            InvalidApprovalTool().definition()


class ToolCallAndResultTests(unittest.TestCase):
    def test_tool_call_normalizes_identifiers_and_preserves_raw_arguments(self):
        call = ToolCall(
            call_id=" call_123 ",
            name=" add ",
            arguments_json='{"left": 1, "right": 2}',
        )

        self.assertEqual(call.call_id, "call_123")
        self.assertEqual(call.name, "add")
        self.assertEqual(call.arguments_json, '{"left": 1, "right": 2}')

    def test_tool_call_rejects_invalid_fields(self):
        with self.assertRaises(ValueError):
            ToolCall(call_id="", name="add", arguments_json="{}")
        with self.assertRaises(ValueError):
            ToolCall(call_id="call_1", name=" ", arguments_json="{}")
        with self.assertRaises(TypeError):
            ToolCall(
                call_id="call_1",
                name="add",
                arguments_json={},  # type: ignore[arg-type]
            )

    def test_result_factories_create_consistent_states(self):
        success = ToolResult.success("call_1", "add", 3)
        failure = ToolResult.failure(
            "call_2",
            "missing",
            "Not registered",
            "tool_not_found",
        )
        approval = ToolResult.approval_required("call_3", "delete_file")

        self.assertEqual(success.status, ToolResultStatus.SUCCESS)
        self.assertEqual(success.output, 3)
        self.assertEqual(failure.status, ToolResultStatus.ERROR)
        self.assertEqual(failure.error_code, "tool_not_found")
        self.assertEqual(approval.status, ToolResultStatus.APPROVAL_REQUIRED)

    def test_result_rejects_inconsistent_states(self):
        invalid_results = [
            {
                "status": ToolResultStatus.SUCCESS,
                "output": None,
            },
            {
                "status": ToolResultStatus.SUCCESS,
                "output": "ok",
                "error": "unexpected",
            },
            {
                "status": ToolResultStatus.ERROR,
                "output": "unexpected",
                "error": "failed",
            },
            {
                "status": ToolResultStatus.ERROR,
                "error": " ",
            },
            {
                "status": ToolResultStatus.APPROVAL_REQUIRED,
                "error_code": "unexpected",
            },
        ]

        for values in invalid_results:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    ToolResult(
                        call_id="call_1",
                        tool_name="tool",
                        **values,
                    )


if __name__ == "__main__":
    unittest.main()
