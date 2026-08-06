import tempfile
import unittest
from pathlib import Path

from truecoder.tools import ToolExecutionError
from truecoder.tools.base import MutatingTool
from truecoder.tools.builtin import (
    EditFileArguments,
    EditFileTool,
    WriteFileArguments,
    WriteFileTool,
)


class WriteFilePreviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._directory.name).resolve()
        self.tool = WriteFileTool(self.workspace)
        self.addCleanup(self._directory.cleanup)

    async def _preview(self, path: str, content: str):
        return await self.tool.preview_mutation(
            WriteFileArguments(path=path, content=content)
        )

    def test_the_tool_satisfies_the_mutating_protocol(self):
        self.assertIsInstance(self.tool, MutatingTool)

    async def test_a_new_file_previews_as_a_creation(self):
        diff = await self._preview("new.py", "one\ntwo\n")

        assert diff is not None
        self.assertEqual(diff.kind, "create")
        self.assertEqual((diff.added, diff.removed), (2, 0))
        self.assertEqual(diff.path, "new.py")

    async def test_an_existing_file_previews_as_a_replacement(self):
        (self.workspace / "a.py").write_bytes(b"one\ntwo\n")

        diff = await self._preview("a.py", "one\nTWO\n")

        assert diff is not None
        self.assertEqual(diff.kind, "replace")
        self.assertEqual((diff.added, diff.removed), (1, 1))

    async def test_an_unchanged_write_previews_as_an_empty_diff(self):
        (self.workspace / "a.py").write_bytes(b"one\n")

        diff = await self._preview("a.py", "one\n")

        assert diff is not None
        self.assertTrue(diff.is_empty)

    async def test_a_non_utf8_file_has_no_preview(self):
        (self.workspace / "a.bin").write_bytes(b"\xff\xfe\x00binary")

        self.assertIsNone(await self._preview("a.bin", "text"))

    async def test_an_oversized_file_has_no_preview(self):
        (self.workspace / "big.txt").write_bytes(b"x" * (1024 * 1024 + 1))

        self.assertIsNone(await self._preview("big.txt", "small"))

    async def test_previewing_does_not_create_or_change_anything(self):
        target = self.workspace / "a.py"
        target.write_bytes(b"original\n")

        await self._preview("a.py", "changed\n")
        await self._preview("brand-new.py", "content\n")

        self.assertEqual(target.read_text(encoding="utf-8"), "original\n")
        self.assertFalse((self.workspace / "brand-new.py").exists())

    async def test_a_rejected_destination_raises_for_the_caller_to_absorb(self):
        with self.assertRaises(ToolExecutionError):
            await self._preview("../escape.py", "content")


class EditFilePreviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._directory.name).resolve()
        self.tool = EditFileTool(self.workspace)
        self.target = self.workspace / "a.py"
        self.target.write_bytes(b"one\ntwo\nthree\n")
        self.addCleanup(self._directory.cleanup)

    async def _preview(self, old: str, new: str, *, replace_all: bool = False):
        return await self.tool.preview_mutation(
            EditFileArguments(
                path="a.py",
                old_text=old,
                new_text=new,
                replace_all=replace_all,
            )
        )

    def test_the_tool_satisfies_the_mutating_protocol(self):
        self.assertIsInstance(self.tool, MutatingTool)

    async def test_a_unique_replacement_previews_as_an_edit(self):
        diff = await self._preview("two", "TWO")

        assert diff is not None
        self.assertEqual(diff.kind, "edit")
        self.assertEqual((diff.added, diff.removed), (1, 1))

    async def test_a_deletion_previews_as_a_removal(self):
        diff = await self._preview("two\n", "")

        assert diff is not None
        self.assertEqual((diff.added, diff.removed), (0, 1))

    async def test_text_that_is_absent_has_no_preview(self):
        self.assertIsNone(await self._preview("missing", "x"))

    async def test_an_ambiguous_match_has_no_preview(self):
        self.target.write_bytes(b"dup\ndup\n")

        self.assertIsNone(await self._preview("dup", "x"))

    async def test_an_ambiguous_match_previews_when_replacing_all(self):
        self.target.write_bytes(b"dup\ndup\n")

        diff = await self._preview("dup", "x", replace_all=True)

        assert diff is not None
        self.assertEqual((diff.added, diff.removed), (2, 2))

    async def test_a_replacement_that_changes_nothing_has_no_preview(self):
        self.assertIsNone(await self._preview("two", "two"))

    async def test_previewing_does_not_change_the_file(self):
        await self._preview("two", "TWO")

        self.assertEqual(
            self.target.read_text(encoding="utf-8"),
            "one\ntwo\nthree\n",
        )


if __name__ == "__main__":
    unittest.main()
