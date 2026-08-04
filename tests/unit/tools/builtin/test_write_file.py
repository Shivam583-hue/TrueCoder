
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers.platforms import requires_posix_permissions, requires_symlinks
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
    MAX_WRITE_BYTES,
    WriteFileArguments,
    WriteFileOutput,
    WriteFileTool,
)


class WriteFileConstructionTests(unittest.TestCase):
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
                with (
                    self.subTest(invalid_root=invalid_root),
                    self.assertRaises(ValueError),
                ):
                    WriteFileTool(invalid_root)

        with self.assertRaises(TypeError):
            WriteFileTool("/workspace")  # type: ignore[arg-type]

    @requires_symlinks
    def test_resolves_and_preserves_the_injected_workspace_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory).resolve()
            workspace = temporary_root / "workspace"
            workspace.mkdir()
            workspace_alias = temporary_root / "workspace-alias"
            workspace_alias.symlink_to(workspace, target_is_directory=True)

            tool = WriteFileTool(workspace_alias)

            self.assertEqual(tool.workspace_root, workspace)


class WriteFileToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name).resolve()
        self.tool = WriteFileTool(self.workspace)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _arguments(path: str, content: str = "content") -> WriteFileArguments:
        return WriteFileArguments(path=path, content=content)

    async def _assert_error_code(
        self,
        expected_code: str,
        arguments: WriteFileArguments,
    ) -> None:
        with self.assertRaises(ToolExecutionError) as caught:
            await self.tool.run(arguments)

        self.assertEqual(caught.exception.code, expected_code)

    def test_definition_has_strict_required_inputs(self):
        function_schema = self.tool.definition().to_chat_completion_schema()[
            "function"
        ]
        parameters = function_schema["parameters"]

        self.assertEqual(function_schema["name"], "write_file")
        self.assertTrue(function_schema["strict"])
        self.assertEqual(parameters["required"], ["path", "content"])
        self.assertEqual(set(parameters["properties"]), {"path", "content"})
        self.assertFalse(parameters["additionalProperties"])

        for property_schema in parameters["properties"].values():
            self.assertNotIn("default", property_schema)

    def test_both_arguments_are_required_and_path_cannot_be_empty(self):
        invalid_arguments = (
            "{}",
            '{"path": "file.txt"}',
            '{"content": "text"}',
            '{"path": "", "content": "text"}',
        )

        for arguments_json in invalid_arguments:
            with (
                self.subTest(arguments_json=arguments_json),
                self.assertRaises(ToolArgumentError),
            ):
                self.tool.parse_arguments(arguments_json)

        self.assertEqual(
            self.tool.parse_arguments('{"path":"empty.txt","content":""}'),
            WriteFileArguments(path="empty.txt", content=""),
        )

    def test_approval_is_explicitly_required(self):
        self.assertIs(self.tool.approval, ToolApproval.REQUIRED)

    async def test_executor_requires_approval_before_writing(self):
        registry = ToolRegistry()
        registry.register(self.tool)
        executor = ToolExecutor(registry)
        call = ToolCall(
            "call_1",
            "write_file",
            '{"path":"created.txt","content":"hello"}',
        )

        pending_result = await executor.execute(call)
        self.assertEqual(
            pending_result.status,
            ToolResultStatus.APPROVAL_REQUIRED,
        )
        self.assertFalse((self.workspace / "created.txt").exists())

        approved_result = await executor.execute(call, approved=True)
        self.assertEqual(approved_result.status, ToolResultStatus.SUCCESS)
        self.assertEqual(
            approved_result.output,
            {
                "path": "created.txt",
                "created": True,
                "bytes_written": 5,
            },
        )
        self.assertEqual(
            (self.workspace / "created.txt").read_text(encoding="utf-8"),
            "hello",
        )

    async def test_creates_exact_utf8_content_without_adding_a_newline(self):
        content = "héllo\r\n世界"

        result = await self.tool.run(self._arguments("unicode.txt", content))

        self.assertEqual(
            result,
            {
                "path": "unicode.txt",
                "created": True,
                "bytes_written": len(content.encode("utf-8")),
            },
        )
        self.assertEqual(
            (self.workspace / "unicode.txt").read_bytes(),
            content.encode("utf-8"),
        )

    async def test_allows_an_empty_file(self):
        result = await self.tool.run(self._arguments("empty.txt", ""))

        self.assertEqual(result["bytes_written"], 0)
        self.assertEqual((self.workspace / "empty.txt").read_bytes(), b"")

    @requires_posix_permissions
    async def test_atomically_replaces_a_file_and_preserves_permissions(self):
        destination = self.workspace / "script.sh"
        destination.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        destination.chmod(0o751)

        result = await self.tool.run(
            self._arguments("script.sh", "#!/bin/sh\nexit 0\n")
        )

        self.assertFalse(result["created"])
        self.assertEqual(
            destination.read_text(encoding="utf-8"),
            "#!/bin/sh\nexit 0\n",
        )
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o751)

    async def test_accepts_the_byte_limit_and_rejects_content_over_it(self):
        exact_ascii = "a" * MAX_WRITE_BYTES
        exact_unicode = "é" * (MAX_WRITE_BYTES // 2)

        for index, content in enumerate((exact_ascii, exact_unicode), start=1):
            with self.subTest(content_type=index):
                result = await self.tool.run(
                    self._arguments(f"limit-{index}.txt", content)
                )
                self.assertEqual(result["bytes_written"], MAX_WRITE_BYTES)

        await self._assert_error_code(
            "content_too_large",
            self._arguments("too-large.txt", f"{exact_ascii}a"),
        )

    async def test_rejects_binary_control_characters(self):
        for character in ("\x00", "\x01", "\x7f"):
            with self.subTest(character=repr(character)):
                await self._assert_error_code(
                    "unsupported_content",
                    self._arguments("binary.txt", f"text{character}"),
                )

    async def test_rejects_absolute_paths_and_workspace_escapes(self):
        outside = self.workspace.parent / "outside.txt"

        for path in (str(outside), "../outside.txt"):
            with self.subTest(path=path):
                await self._assert_error_code(
                    "outside_workspace",
                    self._arguments(path),
                )

        self.assertFalse(outside.exists())

    async def test_rejects_missing_and_non_directory_parents(self):
        await self._assert_error_code(
            "parent_not_found",
            self._arguments("missing/file.txt"),
        )

        parent_file = self.workspace / "parent.txt"
        parent_file.write_text("not a directory", encoding="utf-8")
        await self._assert_error_code(
            "not_a_directory",
            self._arguments("parent.txt/file.txt"),
        )

    @requires_symlinks
    async def test_rejects_symlink_targets_and_parent_directories(self):
        real_directory = self.workspace / "real"
        real_directory.mkdir()
        (self.workspace / "directory-link").symlink_to(
            real_directory,
            target_is_directory=True,
        )
        target = self.workspace / "target.txt"
        target.write_text("unchanged", encoding="utf-8")
        (self.workspace / "file-link").symlink_to(target)

        for path in ("directory-link/file.txt", "file-link"):
            with self.subTest(path=path):
                await self._assert_error_code(
                    "symlink_not_allowed",
                    self._arguments(path),
                )

        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

    async def test_rejects_sensitive_paths_and_allows_environment_templates(self):
        (self.workspace / ".git").mkdir()

        for path in (
            ".env",
            ".git/config",
            "credentials.json",
            "private.key",
        ):
            with self.subTest(path=path):
                await self._assert_error_code(
                    "sensitive_path",
                    self._arguments(path),
                )

        result = await self.tool.run(
            self._arguments(".env.example", "API_KEY=replace-me")
        )
        self.assertTrue(result["created"])

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

    async def test_maps_permission_failures_and_cleans_up_temporary_files(self):
        with patch(
            "truecoder.tools.builtin.write_file.tempfile.mkstemp",
            side_effect=PermissionError,
        ):
            await self._assert_error_code(
                "permission_denied",
                self._arguments("forbidden.txt"),
            )

        self.assertEqual(tuple(self.workspace.glob(".*.tmp")), ())

    async def test_atomic_replace_failure_leaves_original_file_unchanged(self):
        destination = self.workspace / "existing.txt"
        destination.write_text("original", encoding="utf-8")

        with patch(
            "truecoder.tools.builtin.write_file.os.replace",
            side_effect=OSError("replace failed"),
        ):
            await self._assert_error_code(
                "write_failed",
                self._arguments("existing.txt", "replacement"),
            )

        self.assertEqual(destination.read_text(encoding="utf-8"), "original")
        self.assertEqual(tuple(self.workspace.glob(".existing.txt.*.tmp")), ())

    async def test_does_not_consult_the_working_directory(self):
        with patch.object(
            Path,
            "cwd",
            side_effect=AssertionError("run consulted the working directory"),
        ):
            result = await self.tool.run(self._arguments("file.txt"))

        self.assertTrue(result["created"])

    def test_output_contract_contains_path_creation_and_byte_count(self):
        output = WriteFileOutput(
            path="src/example.py",
            created=False,
            bytes_written=42,
        )

        self.assertEqual(
            output,
            {
                "path": "src/example.py",
                "created": False,
                "bytes_written": 42,
            },
        )


if __name__ == "__main__":
    unittest.main()
