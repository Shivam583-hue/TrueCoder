import json
import unittest

from truecoder.tools import ToolResult, serialize_tool_result


class SerializeToolResultTests(unittest.TestCase):
    def test_success_wraps_output_with_status(self):
        result = ToolResult.success("call_1", "read_file", {"content": "print(1)"})

        payload = json.loads(serialize_tool_result(result))

        self.assertEqual(
            payload,
            {"status": "success", "output": {"content": "print(1)"}},
        )

    def test_error_includes_message_and_code(self):
        result = ToolResult.failure(
            "call_1",
            "read_file",
            error="File not found.",
            error_code="file_not_found",
        )

        payload = json.loads(serialize_tool_result(result))

        self.assertEqual(
            payload,
            {
                "status": "error",
                "error": "File not found.",
                "error_code": "file_not_found",
            },
        )

    def test_error_without_code_serializes_null_code(self):
        result = ToolResult.failure("call_1", "read_file", error="Boom.")

        payload = json.loads(serialize_tool_result(result))

        self.assertEqual(payload["error_code"], None)

    def test_approval_required_only_carries_status(self):
        result = ToolResult.approval_required("call_1", "read_file")

        payload = json.loads(serialize_tool_result(result))

        self.assertEqual(payload, {"status": "approval_required"})

    def test_output_is_deterministically_ordered(self):
        result = ToolResult.success("call_1", "read_file", {"b": 2, "a": 1})

        self.assertEqual(
            serialize_tool_result(result),
            '{"output": {"a": 1, "b": 2}, "status": "success"}',
        )


if __name__ == "__main__":
    unittest.main()
