
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers.platforms import requires_symlinks
from truecoder.tools import ToolApproval, ToolArgumentError, ToolExecutionError
from truecoder.tools.builtin import (
    GrepArguments,
    GrepOutput,
    GrepTool,
)


class GrepToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name).resolve()
        self.tool = GrepTool(self.workspace)

    def tearDown(self):
        self.temporary_directory.cleanup()

    async def _assert_error_code(self, code: str, path: str, pattern: str):
        with self.assertRaises(ToolExecutionError) as caught:
            await self.tool.run(GrepArguments(path=path, pattern=pattern))
        self.assertEqual(caught.exception.code, code)

    def test_definition_has_strict_required_inputs_and_requires_approval(self):
        function = self.tool.definition().to_chat_completion_schema()["function"]
        parameters = function["parameters"]

        self.assertEqual(function["name"], "grep")
        self.assertTrue(function["strict"])
        self.assertEqual(parameters["required"], ["path", "pattern"])
        self.assertEqual(set(parameters["properties"]), {"path", "pattern"})
        self.assertFalse(parameters["additionalProperties"])
        self.assertIs(self.tool.approval, ToolApproval.REQUIRED)

        for arguments in ("{}", '{"path":"."}', '{"path":".","pattern":""}'):
            with (
                self.subTest(arguments=arguments),
                self.assertRaises(ToolArgumentError),
            ):
                self.tool.parse_arguments(arguments)

    async def test_searches_a_single_file_with_line_numbers(self):
        (self.workspace / "app.py").write_text(
            "first\nNeedle here\nlast needle\n",
            encoding="utf-8",
        )

        result = await self.tool.run(
            GrepArguments(path="app.py", pattern="Needle")
        )

        self.assertEqual(
            result["matches"],
            [{"path": "app.py", "line_number": 2, "line": "Needle here"}],
        )
        self.assertFalse(result["has_more"])

    async def test_recursively_searches_in_deterministic_path_order(self):
        source = self.workspace / "src"
        source.mkdir()
        (source / "b.py").write_text("target b\n", encoding="utf-8")
        (source / "a.py").write_text("target a\n", encoding="utf-8")
        (self.workspace / "root.txt").write_text("target root\n", encoding="utf-8")

        result = await self.tool.run(GrepArguments(path=".", pattern="target"))

        self.assertEqual(
            [match["path"] for match in result["matches"]],
            ["root.txt", "src/a.py", "src/b.py"],
        )

    async def test_supports_regular_expressions(self):
        (self.workspace / "values.txt").write_text(
            "item-123\nitem-abc\nITEM-456\n",
            encoding="utf-8",
        )

        result = await self.tool.run(
            GrepArguments(path="values.txt", pattern=r"(?i)^item-\d+$")
        )

        self.assertEqual(
            [match["line_number"] for match in result["matches"]],
            [1, 3],
        )

    @requires_symlinks
    async def test_skips_sensitive_linked_binary_and_oversized_files(self):
        (self.workspace / ".env").write_text("target", encoding="utf-8")
        (self.workspace / "binary.bin").write_bytes(b"target\x00")
        (self.workspace / "large.txt").write_text("target", encoding="utf-8")
        (self.workspace / "visible.txt").write_text("target", encoding="utf-8")
        (self.workspace / "visible-link").symlink_to(self.workspace / "visible.txt")

        with patch("truecoder.tools.builtin.grep.MAX_GREP_FILE_BYTES", 3):
            result = await self.tool.run(GrepArguments(path=".", pattern="target"))

        self.assertEqual(result["matches"], [])

        result = await self.tool.run(GrepArguments(path=".", pattern="target"))
        self.assertEqual(
            [match["path"] for match in result["matches"]],
            ["large.txt", "visible.txt"],
        )

    async def test_rejects_invalid_patterns_and_unsafe_paths(self):
        cases = (
            ("invalid_pattern", ".", "["),
            ("outside_workspace", "../outside", "text"),
            ("file_not_found", "missing", "text"),
        )
        for code, path, pattern in cases:
            with self.subTest(path=path, pattern=pattern):
                await self._assert_error_code(code, path, pattern)

    async def test_caps_matches_and_reports_truncation(self):
        (self.workspace / "matches.txt").write_text(
            "match\nmatch\nmatch\n",
            encoding="utf-8",
        )

        with patch("truecoder.tools.builtin.grep.MAX_GREP_MATCHES", 2):
            result = await self.tool.run(
                GrepArguments(path=".", pattern="match")
            )

        self.assertEqual(len(result["matches"]), 2)
        self.assertTrue(result["has_more"])

    async def test_truncates_long_display_lines_after_matching_the_full_line(self):
        (self.workspace / "long.txt").write_text(
            f"{'a' * 600}needle\n",
            encoding="utf-8",
        )

        result = await self.tool.run(
            GrepArguments(path="long.txt", pattern="needle$")
        )

        self.assertEqual(len(result["matches"][0]["line"]), 501)
        self.assertTrue(result["matches"][0]["line"].endswith("…"))

    def test_output_contract_records_query_and_matches(self):
        output = GrepOutput(
            path="src",
            pattern="Agent",
            matches=[
                {
                    "path": "src/agent.py",
                    "line_number": 10,
                    "line": "class Agent:",
                }
            ],
            has_more=False,
        )

        self.assertEqual(output["matches"][0]["line_number"], 10)


if __name__ == "__main__":
    unittest.main()
