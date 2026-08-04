
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers.platforms import requires_symlinks
from truecoder.tools import ToolApproval, ToolArgumentError, ToolExecutionError
from truecoder.tools.builtin import GlobArguments, GlobOutput, GlobTool


class GlobToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name).resolve()
        self.tool = GlobTool(self.workspace)

    def tearDown(self):
        self.temporary_directory.cleanup()

    async def _assert_error_code(self, code: str, path: str, pattern: str):
        with self.assertRaises(ToolExecutionError) as caught:
            await self.tool.run(GlobArguments(path=path, pattern=pattern))
        self.assertEqual(caught.exception.code, code)

    def test_definition_has_strict_required_inputs_and_requires_approval(self):
        function = self.tool.definition().to_chat_completion_schema()["function"]
        parameters = function["parameters"]

        self.assertEqual(function["name"], "glob")
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

    async def test_star_matches_one_level_and_double_star_matches_recursively(self):
        source = self.workspace / "src"
        package = source / "package"
        package.mkdir(parents=True)
        (source / "app.py").write_text("", encoding="utf-8")
        (package / "module.py").write_text("", encoding="utf-8")
        (package / "notes.txt").write_text("", encoding="utf-8")

        shallow = await self.tool.run(GlobArguments(path="src", pattern="*.py"))
        recursive = await self.tool.run(
            GlobArguments(path="src", pattern="**/*.py")
        )

        self.assertEqual(shallow["matches"], ["src/app.py"])
        self.assertEqual(
            recursive["matches"],
            ["src/app.py", "src/package/module.py"],
        )

    async def test_matches_directories_and_character_patterns(self):
        (self.workspace / "src").mkdir()
        (self.workspace / "scripts").mkdir()
        (self.workspace / "tests").mkdir()

        result = await self.tool.run(GlobArguments(path=".", pattern="s*"))

        self.assertEqual(result["matches"], ["scripts", "src"])

    async def test_search_is_relative_to_the_requested_base_directory(self):
        nested = self.workspace / "project" / "src"
        nested.mkdir(parents=True)
        (nested / "app.py").write_text("", encoding="utf-8")

        result = await self.tool.run(
            GlobArguments(path="project", pattern="src/*.py")
        )

        self.assertEqual(result["matches"], ["project/src/app.py"])

    @requires_symlinks
    async def test_skips_sensitive_paths_and_symbolic_links(self):
        (self.workspace / ".git").mkdir()
        (self.workspace / ".git" / "config.py").write_text("", encoding="utf-8")
        (self.workspace / ".env").write_text("", encoding="utf-8")
        (self.workspace / ".env.example").write_text("", encoding="utf-8")
        real = self.workspace / "real"
        real.mkdir()
        (real / "module.py").write_text("", encoding="utf-8")
        (self.workspace / "linked").symlink_to(real, target_is_directory=True)

        result = await self.tool.run(GlobArguments(path=".", pattern="**/*"))

        self.assertEqual(
            result["matches"],
            [".env.example", "real", "real/module.py"],
        )

    async def test_rejects_invalid_patterns_and_unsafe_base_paths(self):
        cases = (
            ("invalid_pattern", ".", "/absolute/*.py"),
            ("invalid_pattern", ".", "../*.py"),
            ("outside_workspace", "../outside", "*.py"),
            ("file_not_found", "missing", "*.py"),
        )
        for code, path, pattern in cases:
            with self.subTest(path=path, pattern=pattern):
                await self._assert_error_code(code, path, pattern)

    async def test_caps_matches_and_reports_truncation(self):
        for name in ("a.py", "b.py", "c.py"):
            (self.workspace / name).write_text("", encoding="utf-8")

        with patch("truecoder.tools.builtin.glob.MAX_GLOB_MATCHES", 2):
            result = await self.tool.run(
                GlobArguments(path=".", pattern="*.py")
            )

        self.assertEqual(result["matches"], ["a.py", "b.py"])
        self.assertTrue(result["has_more"])

    async def test_caps_scanned_entries(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.workspace / name).write_text("", encoding="utf-8")

        with patch("truecoder.tools.builtin.glob.MAX_GLOB_SCANNED_ENTRIES", 2):
            result = await self.tool.run(
                GlobArguments(path=".", pattern="*.txt")
            )

        self.assertEqual(len(result["matches"]), 2)
        self.assertTrue(result["has_more"])

    def test_output_contract_records_query_and_matches(self):
        output = GlobOutput(
            path="src",
            pattern="**/*.py",
            matches=["src/app.py"],
            has_more=False,
        )

        self.assertEqual(output["pattern"], "**/*.py")


if __name__ == "__main__":
    unittest.main()
