import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from truecoder.tools import (
    ToolApproval,
    ToolArgumentError,
    ToolCall,
    ToolExecutionError,
    ToolExecutor,
    ToolRegistry,
    ToolResultStatus,
)
from truecoder.tools.builtin import (
    MAX_LINE_COUNT,
    ReadFileArguments,
    ReadFileOutput,
    ReadFileTool,
)


class ReadFileConstructionTests(unittest.TestCase):
    def test_requires_an_existing_absolute_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory).resolve()
            regular_file = workspace / "file.txt"
            regular_file.write_text("content", encoding="utf-8")

            invalid_roots = [
                Path("relative"),
                workspace / "missing",
                regular_file,
            ]

            for invalid_root in invalid_roots:
                with self.subTest(invalid_root=invalid_root):
                    with self.assertRaises(ValueError):
                        ReadFileTool(invalid_root)

        with self.assertRaises(TypeError):
            ReadFileTool("/workspace")  # type: ignore[arg-type]

    def test_resolves_and_preserves_the_injected_workspace_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory).resolve()
            workspace = temporary_root / "workspace"
            workspace.mkdir()
            workspace_alias = temporary_root / "workspace-alias"
            workspace_alias.symlink_to(workspace, target_is_directory=True)

            tool = ReadFileTool(workspace_alias)

            self.assertEqual(tool.workspace_root, workspace)


class ReadFileToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name).resolve()
        self.tool = ReadFileTool(self.workspace)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _arguments(
        self,
        path: str,
        *,
        start_line: int = 1,
        line_count: int = 100,
    ) -> ReadFileArguments:
        return ReadFileArguments(
            path=path,
            start_line=start_line,
            line_count=line_count,
        )

    async def _assert_error_code(
        self,
        expected_code: str,
        arguments: ReadFileArguments,
    ) -> None:
        with self.assertRaises(ToolExecutionError) as caught:
            await self.tool.run(arguments)

        self.assertEqual(caught.exception.code, expected_code)

    def test_definition_has_strict_required_bounded_inputs(self):
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
        self.assertEqual(
            parameters["properties"]["line_count"]["maximum"],
            MAX_LINE_COUNT,
        )

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

    def test_line_inputs_are_one_based_positive_and_bounded(self):
        invalid_arguments = [
            '{"path": "README.md", "start_line": 0, "line_count": 100}',
            '{"path": "README.md", "start_line": 1, "line_count": 0}',
            (
                '{"path": "README.md", "start_line": 1, '
                f'"line_count": {MAX_LINE_COUNT + 1}}}'
            ),
        ]

        for arguments_json in invalid_arguments:
            with self.subTest(arguments_json=arguments_json):
                with self.assertRaises(ToolArgumentError):
                    self.tool.parse_arguments(arguments_json)

    def test_approval_is_explicitly_required(self):
        self.assertIs(self.tool.approval, ToolApproval.REQUIRED)

    async def test_executor_requires_approval_and_preserves_domain_codes(self):
        registry = ToolRegistry()
        registry.register(self.tool)
        executor = ToolExecutor(registry)
        call = ToolCall(
            "call_1",
            "read_file",
            '{"path": "missing.txt", "start_line": 1, "line_count": 10}',
        )

        pending_result = await executor.execute(call)
        approved_result = await executor.execute(call, approved=True)

        self.assertEqual(
            pending_result.status,
            ToolResultStatus.APPROVAL_REQUIRED,
        )
        self.assertEqual(approved_result.status, ToolResultStatus.ERROR)
        self.assertEqual(approved_result.error_code, "file_not_found")

    async def test_reads_the_requested_range_and_reports_more_content(self):
        source = self.workspace / "source.py"
        source.write_text(
            "line 1\nline 2\nline 3\nline 4\n",
            encoding="utf-8",
        )

        result = await self.tool.run(
            self._arguments("source.py", start_line=2, line_count=2)
        )

        self.assertEqual(
            result,
            {
                "path": "source.py",
                "content": "line 2\nline 3\n",
                "start_line": 2,
                "end_line": 3,
                "has_more": True,
            },
        )

    async def test_reports_the_actual_range_at_and_beyond_end_of_file(self):
        source = self.workspace / "source.py"
        source.write_text("line 1\nline 2", encoding="utf-8")

        final_range = await self.tool.run(
            self._arguments("source.py", start_line=2, line_count=2)
        )
        empty_range = await self.tool.run(
            self._arguments("source.py", start_line=10, line_count=2)
        )

        self.assertEqual(final_range["content"], "line 2")
        self.assertEqual(final_range["end_line"], 2)
        self.assertFalse(final_range["has_more"])
        self.assertEqual(empty_range["content"], "")
        self.assertEqual(empty_range["start_line"], 10)
        self.assertEqual(empty_range["end_line"], 9)
        self.assertFalse(empty_range["has_more"])

    async def test_reads_only_through_one_line_beyond_the_requested_range(self):
        source = self.workspace / "source.py"
        source.write_text(
            "".join(f"line {number}\n" for number in range(1, 20)),
            encoding="utf-8",
        )
        yielded_lines = 0

        class CountingFile:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def __iter__(self):
                return self

            def __next__(self):
                nonlocal yielded_lines
                yielded_lines += 1
                if yielded_lines > 3:
                    raise AssertionError(
                        "read_file consumed more than one extra line"
                    )
                return f"line {yielded_lines}\n".encode()

        with patch.object(Path, "open", return_value=CountingFile()):
            result = await self.tool.run(
                self._arguments("source.py", start_line=1, line_count=2)
            )

        self.assertEqual(yielded_lines, 3)
        self.assertTrue(result["has_more"])

    async def test_does_not_consult_the_working_directory_during_run(self):
        source = self.workspace / "source.py"
        source.write_text("content", encoding="utf-8")

        with patch.object(
            Path,
            "cwd",
            side_effect=AssertionError("run consulted the working directory"),
        ):
            result = await self.tool.run(self._arguments("source.py"))

        self.assertEqual(result["content"], "content")

    async def test_rejects_absolute_parent_and_symlink_escapes(self):
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory).resolve()
            outside_file = outside / "outside.txt"
            outside_file.write_text("secret", encoding="utf-8")
            (self.workspace / "escape").symlink_to(outside_file)

            paths = [
                str(outside_file),
                "../outside.txt",
                "escape",
            ]

            for path in paths:
                with self.subTest(path=path):
                    await self._assert_error_code(
                        "outside_workspace",
                        self._arguments(path),
                    )

    async def test_returns_file_not_found_for_a_missing_file(self):
        await self._assert_error_code(
            "file_not_found",
            self._arguments("missing.txt"),
        )

    async def test_rejects_directories_and_non_regular_files(self):
        (self.workspace / "directory").mkdir()
        await self._assert_error_code(
            "not_a_file",
            self._arguments("directory"),
        )

        if hasattr(os, "mkfifo"):
            os.mkfifo(self.workspace / "pipe")
            await self._assert_error_code(
                "not_a_file",
                self._arguments("pipe"),
            )

    async def test_rejects_sensitive_paths_and_symlinks_to_them(self):
        sensitive_paths = [
            ".env",
            ".git/config",
            "credentials.json",
            "private.key",
            ".ssh/config",
        ]

        for path in sensitive_paths:
            target = self.workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("secret", encoding="utf-8")

            with self.subTest(path=path):
                await self._assert_error_code(
                    "sensitive_path",
                    self._arguments(path),
                )

        (self.workspace / "env-link").symlink_to(self.workspace / ".env")
        await self._assert_error_code(
            "sensitive_path",
            self._arguments("env-link"),
        )

    async def test_allows_non_secret_environment_templates(self):
        template = self.workspace / ".env.example"
        template.write_text("API_KEY=replace-me", encoding="utf-8")

        result = await self.tool.run(self._arguments(".env.example"))

        self.assertEqual(result["content"], "API_KEY=replace-me")

    async def test_returns_unsupported_encoding_for_invalid_utf8_and_binary(self):
        invalid_utf8 = self.workspace / "invalid.txt"
        invalid_utf8.write_bytes(b"\xff\xfe")
        binary = self.workspace / "binary.txt"
        binary.write_bytes(b"text\x00binary")

        for path in ["invalid.txt", "binary.txt"]:
            with self.subTest(path=path):
                await self._assert_error_code(
                    "unsupported_encoding",
                    self._arguments(path),
                )

    async def test_returns_permission_denied_when_open_is_forbidden(self):
        source = self.workspace / "source.py"
        source.write_text("content", encoding="utf-8")

        with patch.object(Path, "open", side_effect=PermissionError):
            await self._assert_error_code(
                "permission_denied",
                self._arguments("source.py"),
            )

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
