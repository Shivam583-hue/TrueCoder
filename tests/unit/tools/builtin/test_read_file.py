import unittest

from truecoder.tools import ToolArgumentError
from truecoder.tools.builtin import (
    ReadFileArguments,
    ReadFileOutput,
    ReadFileTool,
)


class StubReadFileTool(ReadFileTool):
    async def run(self, arguments: ReadFileArguments) -> ReadFileOutput:
        return {
            "path": arguments.path,
            "content": "",
            "start_line": arguments.start_line,
            "end_line": arguments.start_line - 1,
            "has_more": False,
        }


class ReadFileContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = StubReadFileTool()

    def test_definition_has_the_read_file_name_and_strict_required_inputs(self):
        function_schema = self.tool.definition().to_chat_completion_schema()[
            "function"
        ]
        parameters = function_schema["parameters"]

        self.assertEqual(function_schema["name"], "read_file")
        self.assertTrue(function_schema["strict"])
        self.assertEqual(
            parameters["required"],
            ["path", "start_line", "line_count"],
        )
        self.assertEqual(
            set(parameters["properties"]),
            {"path", "start_line", "line_count"},
        )
        self.assertFalse(parameters["additionalProperties"])

        for property_schema in parameters["properties"].values():
            self.assertNotIn("default", property_schema)

    def test_all_arguments_must_be_supplied(self):
        incomplete_arguments = [
            '{"start_line": 1, "line_count": 100}',
            '{"path": "README.md", "line_count": 100}',
            '{"path": "README.md", "start_line": 1}',
        ]

        for arguments_json in incomplete_arguments:
            with self.subTest(arguments_json=arguments_json):
                with self.assertRaises(ToolArgumentError):
                    self.tool.parse_arguments(arguments_json)

    def test_line_inputs_are_one_based_and_positive(self):
        invalid_arguments = [
            '{"path": "README.md", "start_line": 0, "line_count": 100}',
            '{"path": "README.md", "start_line": 1, "line_count": 0}',
        ]

        for arguments_json in invalid_arguments:
            with self.subTest(arguments_json=arguments_json):
                with self.assertRaises(ToolArgumentError):
                    self.tool.parse_arguments(arguments_json)

    def test_output_contract_contains_path_content_range_and_continuation(self):
        output = ReadFileOutput(
            path="src/truecoder/tools/base.py",
            content="first line\nsecond line",
            start_line=10,
            end_line=11,
            has_more=True,
        )

        self.assertEqual(
            output,
            {
                "path": "src/truecoder/tools/base.py",
                "content": "first line\nsecond line",
                "start_line": 10,
                "end_line": 11,
                "has_more": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
