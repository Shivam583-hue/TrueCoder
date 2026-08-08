"""Several edits in one call apply together, or not at all."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from truecoder.tools.base import ToolExecutionError
from truecoder.tools.builtin import Edit, EditFileArguments, EditFileTool
from truecoder.tools.builtin.edit_file import (
    MAX_EDITS_PER_CALL,
    EditRejected,
    apply_edits,
)

SOURCE = "alpha\nbeta\ngamma\nbeta\n"


def _edit(old: str, new: str, *, replace_all: bool = False) -> Edit:
    return Edit(old_text=old, new_text=new, replace_all=replace_all)


class ApplyEditsTests(unittest.TestCase):
    def test_one_edit_behaves_as_before(self):
        edited, replacements = apply_edits(SOURCE, [_edit("alpha", "ALPHA")])

        self.assertEqual(edited, "ALPHA\nbeta\ngamma\nbeta\n")
        self.assertEqual(replacements, 1)

    def test_edits_apply_in_order(self):
        edited, _ = apply_edits(
            "a\n",
            [_edit("a", "b"), _edit("b", "c")],
        )

        self.assertEqual(edited, "c\n")

    def test_replacements_are_counted_across_edits(self):
        _edited, replacements = apply_edits(
            SOURCE,
            [_edit("alpha", "A"), _edit("beta", "B", replace_all=True)],
        )

        self.assertEqual(replacements, 3)

    def test_an_ambiguous_edit_names_its_position(self):
        with self.assertRaises(EditRejected) as caught:
            apply_edits(SOURCE, [_edit("alpha", "A"), _edit("beta", "B")])

        self.assertEqual(caught.exception.code, "ambiguous_match")
        self.assertIn("Edit 2 of 2", caught.exception.message)

    def test_a_missing_edit_names_its_position(self):
        with self.assertRaises(EditRejected) as caught:
            apply_edits(SOURCE, [_edit("alpha", "A"), _edit("absent", "X")])

        self.assertEqual(caught.exception.code, "text_not_found")
        self.assertIn("Edit 2 of 2", caught.exception.message)

    def test_a_no_op_series_is_refused(self):
        with self.assertRaises(EditRejected) as caught:
            apply_edits("a\n", [_edit("a", "b"), _edit("b", "a")])

        self.assertEqual(caught.exception.code, "no_change")

    def test_a_later_edit_may_target_earlier_output(self):
        edited, _ = apply_edits(
            "one\n",
            [_edit("one", "two three"), _edit("three", "four")],
        )

        self.assertEqual(edited, "two four\n")


class MultiEditToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)
        self.tool = EditFileTool(self.workspace)
        self.target = self.workspace / "a.py"
        self.target.write_bytes(SOURCE.encode("utf-8"))

    def _arguments(self, *edits: Edit) -> EditFileArguments:
        return EditFileArguments(path="a.py", edits=list(edits))

    async def test_several_edits_land_in_one_call(self):
        result = await self.tool.run(
            self._arguments(
                _edit("alpha", "ALPHA"),
                _edit("gamma", "GAMMA"),
            )
        )

        self.assertEqual(result["edits_applied"], 2)
        self.assertEqual(
            self.target.read_text(encoding="utf-8"),
            "ALPHA\nbeta\nGAMMA\nbeta\n",
        )

    async def test_a_failing_edit_leaves_the_file_untouched(self):
        with self.assertRaises(ToolExecutionError) as caught:
            await self.tool.run(
                self._arguments(
                    _edit("alpha", "ALPHA"),
                    _edit("absent", "X"),
                )
            )

        self.assertEqual(caught.exception.code, "text_not_found")
        self.assertEqual(self.target.read_text(encoding="utf-8"), SOURCE)

    async def test_the_preview_covers_every_edit(self):
        diff = await self.tool.preview_mutation(
            self._arguments(_edit("alpha", "ALPHA"), _edit("gamma", "GAMMA"))
        )

        assert diff is not None
        rendered = "\n".join(line.text for hunk in diff.hunks for line in hunk.lines)
        self.assertIn("ALPHA", rendered)
        self.assertIn("GAMMA", rendered)
        self.assertEqual(diff.added, 2)

    async def test_a_failing_series_previews_nothing(self):
        diff = await self.tool.preview_mutation(
            self._arguments(_edit("alpha", "ALPHA"), _edit("absent", "X"))
        )

        self.assertIsNone(diff)

    async def test_at_least_one_edit_is_required(self):
        with self.assertRaises(ValidationError):
            EditFileArguments(path="a.py", edits=[])

    async def test_too_many_edits_are_refused(self):
        edits = [_edit(f"x{n}", f"y{n}") for n in range(MAX_EDITS_PER_CALL + 1)]

        with self.assertRaises(ValidationError):
            EditFileArguments(path="a.py", edits=edits)

    async def test_replace_all_defaults_to_false(self):
        arguments = self.tool.parse_arguments(
            json.dumps(
                {"path": "a.py", "edits": [{"old_text": "alpha", "new_text": "A"}]}
            )
        )

        self.assertFalse(arguments.edits[0].replace_all)


if __name__ == "__main__":
    unittest.main()
