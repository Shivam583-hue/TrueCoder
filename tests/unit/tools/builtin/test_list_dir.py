import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from truecoder.tools import (
    ToolApproval,
    ToolArgumentError,
    ToolExecutionError,
)
from truecoder.tools.builtin import (
    ListDirArguments,
    ListDirOutput,
    ListDirTool,
)


class ListDirConstructionTests(unittest.TestCase):
    def test_requires_an_existing_absolute_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory).resolve()
            file = workspace / "file.txt"
            file.write_text("content", encoding="utf-8")

            for invalid_root in (
                Path("relative"),
                workspace / "missing",
                file,
            ):
                with (
                    self.subTest(invalid_root=invalid_root),
                    self.assertRaises(ValueError),
                ):
                    ListDirTool(invalid_root)

        with self.assertRaises(TypeError):
            ListDirTool("/workspace")  # type: ignore[arg-type]

    def test_resolves_the_injected_workspace_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            alias = root / "alias"
            alias.symlink_to(workspace, target_is_directory=True)

            self.assertEqual(ListDirTool(alias).workspace_root, workspace)


class ListDirToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name).resolve()
        self.tool = ListDirTool(self.workspace)

    def tearDown(self):
        self.temporary_directory.cleanup()

    async def _assert_error_code(self, code: str, path: str):
        with self.assertRaises(ToolExecutionError) as caught:
            await self.tool.run(ListDirArguments(path=path))
        self.assertEqual(caught.exception.code, code)

    def test_definition_has_one_strict_required_input(self):
        function = self.tool.definition().to_chat_completion_schema()["function"]
        parameters = function["parameters"]

        self.assertEqual(function["name"], "list_dir")
        self.assertTrue(function["strict"])
        self.assertEqual(parameters["required"], ["path"])
        self.assertEqual(set(parameters["properties"]), {"path"})
        self.assertFalse(parameters["additionalProperties"])
        self.assertNotIn("default", parameters["properties"]["path"])

        for arguments in ("{}", '{"path":""}', '{"path":".","extra":true}'):
            with (
                self.subTest(arguments=arguments),
                self.assertRaises(ToolArgumentError),
            ):
                self.tool.parse_arguments(arguments)

    def test_requires_approval(self):
        self.assertIs(self.tool.approval, ToolApproval.REQUIRED)

    async def test_lists_only_immediate_entries_with_directories_first(self):
        (self.workspace / "z-directory").mkdir()
        (self.workspace / "z-directory" / "nested.txt").write_text(
            "nested",
            encoding="utf-8",
        )
        (self.workspace / "A-file.txt").write_text("a", encoding="utf-8")
        (self.workspace / "b-file.txt").write_text("b", encoding="utf-8")

        result = await self.tool.run(ListDirArguments(path="."))

        self.assertEqual(
            [(entry["name"], entry["type"]) for entry in result["entries"]],
            [
                ("z-directory", "directory"),
                ("A-file.txt", "file"),
                ("b-file.txt", "file"),
            ],
        )
        self.assertNotIn(
            "z-directory/nested.txt",
            [entry["path"] for entry in result["entries"]],
        )
        self.assertFalse(result["has_more"])

    async def test_returns_workspace_relative_paths_for_a_subdirectory(self):
        directory = self.workspace / "src"
        directory.mkdir()
        (directory / "app.py").write_text("", encoding="utf-8")

        result = await self.tool.run(ListDirArguments(path="src"))

        self.assertEqual(result["path"], "src")
        self.assertEqual(result["entries"][0]["path"], "src/app.py")

    async def test_hides_sensitive_entries_but_keeps_environment_templates(self):
        (self.workspace / ".git").mkdir()
        (self.workspace / ".env").write_text("SECRET=true", encoding="utf-8")
        (self.workspace / "credentials.json").write_text("{}", encoding="utf-8")
        (self.workspace / ".env.example").write_text("SECRET=", encoding="utf-8")
        (self.workspace / "visible.txt").write_text("", encoding="utf-8")

        result = await self.tool.run(ListDirArguments(path="."))

        self.assertEqual(
            {entry["name"] for entry in result["entries"]},
            {".env.example", "visible.txt"},
        )

    async def test_reports_symlinks_without_following_them(self):
        directory = self.workspace / "directory"
        directory.mkdir()
        (self.workspace / "directory-link").symlink_to(
            directory,
            target_is_directory=True,
        )

        result = await self.tool.run(ListDirArguments(path="."))

        link = next(
            entry for entry in result["entries"] if entry["name"] == "directory-link"
        )
        self.assertEqual(link["type"], "symlink")
        await self._assert_error_code("symlink_not_allowed", "directory-link")

    async def test_rejects_unsafe_missing_and_non_directory_paths(self):
        (self.workspace / "file.txt").write_text("", encoding="utf-8")

        cases = (
            ("outside_workspace", str(self.workspace.parent)),
            ("outside_workspace", "../outside"),
            ("file_not_found", "missing"),
            ("not_a_directory", "file.txt"),
        )
        for code, path in cases:
            with self.subTest(path=path):
                await self._assert_error_code(code, path)

    async def test_caps_results_and_reports_that_more_entries_exist(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.workspace / name).write_text("", encoding="utf-8")

        with patch("truecoder.tools.builtin.list_dir.MAX_DIRECTORY_ENTRIES", 2):
            result = await self.tool.run(ListDirArguments(path="."))

        self.assertEqual(len(result["entries"]), 2)
        self.assertTrue(result["has_more"])

    async def test_does_not_consult_the_working_directory(self):
        with patch.object(
            Path,
            "cwd",
            side_effect=AssertionError("run consulted the working directory"),
        ):
            result = await self.tool.run(ListDirArguments(path="."))

        self.assertEqual(result["path"], ".")

    def test_output_contract_contains_entries_and_truncation_state(self):
        output = ListDirOutput(
            path="src",
            entries=[
                {
                    "path": "src/truecoder",
                    "name": "truecoder",
                    "type": "directory",
                }
            ],
            has_more=False,
        )

        self.assertEqual(output["entries"][0]["type"], "directory")


if __name__ == "__main__":
    unittest.main()
