import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers.platforms import requires_posix_permissions, requires_symlinks
from truecoder.tools import (
    ToolApproval,
    ToolArgumentError,
    ToolExecutionError,
)
from truecoder.tools.builtin import (
    MAX_EDIT_TEXT_BYTES,
    Edit,
    EditFileArguments,
    EditFileOutput,
    EditFileTool,
)


class EditFileToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name).resolve()
        self.tool = EditFileTool(self.workspace)

    def tearDown(self):
        self.temporary_directory.cleanup()

    @staticmethod
    def _arguments(
        path: str = "file.txt",
        old_text: str = "old",
        new_text: str = "new",
        *,
        replace_all: bool = False,
    ) -> EditFileArguments:
        return EditFileArguments(
            path=path,
            edits=[
                Edit(
                    old_text=old_text,
                    new_text=new_text,
                    replace_all=replace_all,
                )
            ],
        )

    async def _assert_error_code(
        self,
        code: str,
        arguments: EditFileArguments,
    ):
        with self.assertRaises(ToolExecutionError) as caught:
            await self.tool.run(arguments)
        self.assertEqual(caught.exception.code, code)

    def test_definition_has_strict_required_inputs_and_requires_approval(self):
        function = self.tool.definition().to_chat_completion_schema()["function"]
        parameters = function["parameters"]

        self.assertEqual(function["name"], "edit_file")
        self.assertTrue(function["strict"])
        self.assertEqual(parameters["required"], ["path", "edits"])
        self.assertEqual(set(parameters["properties"]), {"path", "edits"})
        edit = parameters["$defs"]["Edit"]
        self.assertEqual(
            set(edit["properties"]), {"old_text", "new_text", "replace_all"}
        )
        self.assertFalse(edit["additionalProperties"])
        self.assertFalse(parameters["additionalProperties"])
        self.assertIs(self.tool.approval, ToolApproval.REQUIRED)

        for arguments in (
            "{}",
            '{"path":"file","old_text":"","new_text":"x","replace_all":false}',
            '{"path":"file","old_text":"x","new_text":"y"}',
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaises(ToolArgumentError),
            ):
                self.tool.parse_arguments(arguments)

    @requires_posix_permissions
    async def test_replaces_one_unique_exact_match_and_preserves_permissions(self):
        destination = self.workspace / "script.sh"
        destination.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        destination.chmod(0o751)

        result = await self.tool.run(
            self._arguments(
                "script.sh",
                "exit 1",
                "exit 0",
            )
        )

        self.assertEqual(
            result,
            {
                "path": "script.sh",
                "edits_applied": 1,
                "replacements": 1,
                "bytes_written": len("#!/bin/sh\nexit 0\n"),
            },
        )
        self.assertEqual(
            destination.read_text(encoding="utf-8"),
            "#!/bin/sh\nexit 0\n",
        )
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o751)

    async def test_replace_all_changes_every_exact_occurrence(self):
        destination = self.workspace / "values.txt"
        destination.write_text("old old older", encoding="utf-8")

        result = await self.tool.run(self._arguments("values.txt", replace_all=True))

        self.assertEqual(result["replacements"], 3)
        self.assertEqual(destination.read_text(encoding="utf-8"), "new new newer")

    async def test_empty_new_text_deletes_the_old_text(self):
        destination = self.workspace / "notes.txt"
        destination.write_text("keep remove keep", encoding="utf-8")

        await self.tool.run(self._arguments("notes.txt", " remove", ""))

        self.assertEqual(destination.read_text(encoding="utf-8"), "keep keep")

    async def test_rejects_missing_and_ambiguous_old_text_without_changing_file(self):
        destination = self.workspace / "file.txt"
        destination.write_text("same same", encoding="utf-8")

        await self._assert_error_code(
            "text_not_found",
            self._arguments(old_text="missing"),
        )
        await self._assert_error_code(
            "ambiguous_match",
            self._arguments(old_text="same"),
        )

        self.assertEqual(destination.read_text(encoding="utf-8"), "same same")

    async def test_rejects_no_op_edits_and_unsupported_control_text(self):
        (self.workspace / "file.txt").write_text("same", encoding="utf-8")

        await self._assert_error_code(
            "no_change",
            self._arguments(old_text="same", new_text="same"),
        )
        await self._assert_error_code(
            "unsupported_content",
            self._arguments(old_text="same", new_text="bad\x00"),
        )

    async def test_enforces_edit_fragment_and_resulting_file_byte_limits(self):
        (self.workspace / "file.txt").write_text("old", encoding="utf-8")

        await self._assert_error_code(
            "edit_too_large",
            self._arguments(old_text="a" * (MAX_EDIT_TEXT_BYTES + 1)),
        )

        with patch(
            "truecoder.tools.builtin.edit_file.MAX_EDIT_FILE_BYTES",
            3,
        ):
            await self._assert_error_code(
                "file_too_large",
                self._arguments(new_text="long"),
            )

    @requires_symlinks
    async def test_rejects_unsafe_sensitive_missing_and_symlink_paths(self):
        target = self.workspace / "target.txt"
        target.write_text("old", encoding="utf-8")
        (self.workspace / "link.txt").symlink_to(target)

        cases = (
            ("outside_workspace", self._arguments("../outside.txt")),
            ("sensitive_path", self._arguments(".env")),
            ("file_not_found", self._arguments("missing.txt")),
            ("symlink_not_allowed", self._arguments("link.txt")),
        )
        for code, arguments in cases:
            with self.subTest(path=arguments.path):
                await self._assert_error_code(code, arguments)

    async def test_rejects_non_utf8_and_binary_files(self):
        (self.workspace / "invalid.txt").write_bytes(b"\xffold")
        (self.workspace / "binary.txt").write_bytes(b"old\x00")

        await self._assert_error_code(
            "unsupported_encoding",
            self._arguments("invalid.txt"),
        )
        await self._assert_error_code(
            "unsupported_encoding",
            self._arguments("binary.txt"),
        )

    async def test_atomic_failure_leaves_original_file_and_no_temporary_file(self):
        destination = self.workspace / "file.txt"
        destination.write_text("old", encoding="utf-8")

        with patch(
            "truecoder.tools.builtin.edit_file.os.replace",
            side_effect=OSError("failed"),
        ):
            await self._assert_error_code("edit_failed", self._arguments())

        self.assertEqual(destination.read_text(encoding="utf-8"), "old")
        self.assertEqual(tuple(self.workspace.glob(".file.txt.*.tmp")), ())

    async def test_detects_a_concurrent_file_change_before_replacement(self):
        destination = self.workspace / "file.txt"
        destination.write_text("old", encoding="utf-8")
        original_resolver = (
            "truecoder.tools.builtin.edit_file.resolve_existing_workspace_path"
        )

        call_count = 0

        def resolve_then_change(*args, **kwargs):
            nonlocal call_count
            from truecoder.tools.builtin.filesystem import (
                resolve_existing_workspace_path,
            )

            resolved = resolve_existing_workspace_path(*args, **kwargs)
            call_count += 1
            if call_count == 2:
                destination.write_text("changed elsewhere", encoding="utf-8")
            return resolved

        with patch(original_resolver, side_effect=resolve_then_change):
            await self._assert_error_code("file_changed", self._arguments())

        self.assertEqual(
            destination.read_text(encoding="utf-8"),
            "changed elsewhere",
        )

    async def test_does_not_consult_the_working_directory(self):
        (self.workspace / "file.txt").write_text("old", encoding="utf-8")

        with patch.object(
            Path,
            "cwd",
            side_effect=AssertionError("run consulted the working directory"),
        ):
            result = await self.tool.run(self._arguments())

        self.assertEqual(result["replacements"], 1)

    def test_output_contract_records_replacement_and_byte_counts(self):
        output = EditFileOutput(
            path="src/app.py",
            replacements=1,
            bytes_written=42,
        )

        self.assertEqual(output["bytes_written"], 42)


if __name__ == "__main__":
    unittest.main()
