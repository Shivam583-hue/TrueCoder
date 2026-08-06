import unittest

from truecoder.mutation import (
    MAX_DIFF_LINE_LENGTH,
    DiffHunk,
    DiffLine,
    FileDiff,
    build_file_diff,
)


def _kinds(diff: FileDiff) -> list[str]:
    return [line.kind for hunk in diff.hunks for line in hunk.lines]


def _texts(diff: FileDiff, kind: str) -> list[str]:
    return [
        line.text
        for hunk in diff.hunks
        for line in hunk.lines
        if line.kind == kind
    ]


class BuildFileDiffTests(unittest.TestCase):
    def test_identical_text_produces_no_hunks(self):
        diff = build_file_diff("a.py", "one\ntwo\n", "one\ntwo\n", kind="replace")

        self.assertEqual(diff.hunks, ())
        self.assertEqual((diff.added, diff.removed), (0, 0))
        self.assertTrue(diff.is_empty)

    def test_a_created_file_is_all_additions(self):
        diff = build_file_diff("a.py", "", "one\ntwo\n", kind="create")

        self.assertEqual(_kinds(diff), ["added", "added"])
        self.assertEqual((diff.added, diff.removed), (2, 0))
        self.assertEqual(diff.kind, "create")

    def test_a_replaced_line_shows_removal_before_addition(self):
        diff = build_file_diff("a.py", "one\ntwo\n", "one\nTWO\n", kind="edit")

        self.assertEqual(_kinds(diff), ["context", "removed", "added"])
        self.assertEqual(_texts(diff, "removed"), ["two"])
        self.assertEqual(_texts(diff, "added"), ["TWO"])
        self.assertEqual((diff.added, diff.removed), (1, 1))

    def test_line_numbers_track_both_sides(self):
        diff = build_file_diff("a.py", "one\ntwo\n", "one\nTWO\n", kind="edit")
        lines = diff.hunks[0].lines

        self.assertEqual((lines[0].before_number, lines[0].after_number), (1, 1))
        self.assertEqual((lines[1].before_number, lines[1].after_number), (2, None))
        self.assertEqual((lines[2].before_number, lines[2].after_number), (None, 2))

    def test_deletions_are_counted_without_additions(self):
        diff = build_file_diff("a.py", "one\ntwo\nthree\n", "one\nthree\n", kind="edit")

        self.assertEqual((diff.added, diff.removed), (0, 1))
        self.assertEqual(_texts(diff, "removed"), ["two"])

    def test_distant_changes_produce_separate_hunks(self):
        before = "\n".join(str(number) for number in range(1, 41))
        after = before.replace("2\n", "TWO\n", 1).replace("38\n", "THIRTY8\n", 1)

        diff = build_file_diff("a.py", before, after, kind="edit")

        self.assertEqual(len(diff.hunks), 2)

    def test_context_lines_surround_each_change(self):
        before = "\n".join(str(number) for number in range(1, 21))
        after = before.replace("10\n", "TEN\n", 1)

        diff = build_file_diff("a.py", before, after, kind="edit", context_lines=2)
        lines = diff.hunks[0].lines

        self.assertEqual(
            [line.kind for line in lines],
            ["context", "context", "removed", "added", "context", "context"],
        )

    def test_a_hunk_header_describes_both_ranges(self):
        before = "\n".join(str(number) for number in range(1, 21))
        after = before.replace("10\n", "TEN\n", 1)

        header = build_file_diff("a.py", before, after, kind="edit").hunks[0].header

        self.assertTrue(header.startswith("@@ -"))
        self.assertIn("+", header)
        self.assertTrue(header.endswith("@@"))

    def test_long_lines_are_truncated_for_display(self):
        long_line = "x" * (MAX_DIFF_LINE_LENGTH + 50)

        diff = build_file_diff("a.py", "", long_line, kind="create")

        rendered = _texts(diff, "added")[0]
        self.assertEqual(len(rendered), MAX_DIFF_LINE_LENGTH + 1)
        self.assertTrue(rendered.endswith("…"))

    def test_the_rendered_line_count_is_bounded(self):
        after = "\n".join(f"line {number}" for number in range(500))

        diff = build_file_diff("a.py", "", after, kind="create", max_lines=40)

        self.assertEqual(len(_kinds(diff)), 40)
        self.assertTrue(diff.truncated)

    def test_counts_reflect_the_whole_change_even_when_truncated(self):
        after = "\n".join(f"line {number}" for number in range(500))

        diff = build_file_diff("a.py", "", after, kind="create", max_lines=40)

        self.assertEqual(diff.added, 500)

    def test_an_untruncated_diff_is_not_marked_truncated(self):
        diff = build_file_diff("a.py", "one\n", "two\n", kind="edit")

        self.assertFalse(diff.truncated)

    def test_a_trailing_newline_change_is_reported(self):
        diff = build_file_diff("a.py", "one\n", "one", kind="edit")

        self.assertEqual(diff.hunks, ())
        self.assertTrue(diff.newline_changed)
        self.assertFalse(diff.is_empty)

    def test_a_created_file_does_not_report_a_newline_change(self):
        diff = build_file_diff("a.py", "", "one", kind="create")

        self.assertFalse(diff.newline_changed)

    def test_a_crlf_to_lf_rewrite_is_not_reported_as_unchanged(self):
        diff = build_file_diff(
            "a.py",
            "one\r\ntwo\r\nthree\r\n",
            "one\ntwo\nthree\n",
            kind="replace",
        )

        self.assertTrue(diff.line_endings_changed)
        self.assertFalse(diff.is_empty)

    def test_an_lf_to_crlf_rewrite_is_not_reported_as_unchanged(self):
        diff = build_file_diff(
            "a.py",
            "one\ntwo\n",
            "one\r\ntwo\r\n",
            kind="replace",
        )

        self.assertTrue(diff.line_endings_changed)
        self.assertFalse(diff.is_empty)

    def test_matching_line_endings_report_no_change(self):
        diff = build_file_diff("a.py", "one\r\ntwo\r\n", "one\r\ntwo\r\n", kind="edit")

        self.assertFalse(diff.line_endings_changed)
        self.assertTrue(diff.is_empty)

    def test_a_content_change_is_not_labelled_a_line_ending_change(self):
        diff = build_file_diff("a.py", "one\ntwo\n", "one\nTWO\n", kind="edit")

        self.assertFalse(diff.line_endings_changed)

    def test_a_trailing_newline_change_is_not_labelled_a_line_ending_change(self):
        diff = build_file_diff("a.py", "one\n", "one", kind="edit")

        self.assertTrue(diff.newline_changed)
        self.assertFalse(diff.line_endings_changed)

    def test_any_real_change_renders_as_something(self):
        pairs = [
            ("one\n", "two\n"),
            ("one\r\n", "one\n"),
            ("one\n", "one\r\n"),
            ("one\n", "one"),
            ("one", "one\n"),
            ("one\r\ntwo\r\n", "one\ntwo\n"),
            ("a\nb\nc\n", "a\nc\n"),
            ("", "one\n"),
            ("one\rtwo\r", "one\ntwo\n"),
            ("one\n\n", "one\n"),
        ]

        for before, after in pairs:
            with self.subTest(before=before, after=after):
                diff = build_file_diff("a.py", before, after, kind="replace")

                self.assertNotEqual(before, after)
                self.assertFalse(
                    diff.is_empty,
                    f"a change from {before!r} to {after!r} rendered as unchanged",
                )

    def test_repeated_lines_still_align_in_a_long_file(self):
        before = "\n".join(["def f():", "    pass", ""] * 90)
        after = before.replace("def f():", "def g():", 1)

        diff = build_file_diff("a.py", before, after, kind="edit")

        self.assertEqual(diff.added, 1)
        self.assertEqual(diff.removed, 1)

    def test_the_summary_reports_both_counts(self):
        diff = build_file_diff("a.py", "one\ntwo\n", "one\nTWO\nthree\n", kind="edit")

        self.assertEqual(diff.summary, "+2  -1")

    def test_a_truncated_summary_says_so(self):
        after = "\n".join(f"line {number}" for number in range(500))

        diff = build_file_diff("a.py", "", after, kind="create", max_lines=10)

        self.assertIn("truncated", diff.summary)

    def test_non_text_input_is_rejected(self):
        with self.assertRaises(TypeError):
            build_file_diff("a.py", b"one", "two", kind="edit")  # type: ignore[arg-type]

    def test_invalid_bounds_are_rejected(self):
        with self.assertRaises(ValueError):
            build_file_diff("a.py", "a", "b", kind="edit", max_lines=0)
        with self.assertRaises(ValueError):
            build_file_diff("a.py", "a", "b", kind="edit", context_lines=-1)


class DiffModelTests(unittest.TestCase):
    def test_an_added_line_cannot_carry_an_original_number(self):
        with self.assertRaises(ValueError):
            DiffLine(kind="added", text="x", before_number=3, after_number=3)

    def test_a_removed_line_cannot_carry_a_result_number(self):
        with self.assertRaises(ValueError):
            DiffLine(kind="removed", text="x", before_number=3, after_number=3)

    def test_an_unknown_line_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            DiffLine(kind="moved", text="x", before_number=1, after_number=1)  # type: ignore[arg-type]

    def test_an_empty_hunk_is_rejected(self):
        with self.assertRaises(ValueError):
            DiffHunk(
                before_start=1,
                before_count=0,
                after_start=1,
                after_count=0,
                lines=(),
            )

    def test_a_diff_requires_a_path(self):
        with self.assertRaises(ValueError):
            FileDiff(path="  ", kind="edit", hunks=(), added=0, removed=0)

    def test_an_unknown_mutation_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            FileDiff(path="a.py", kind="rename", hunks=(), added=0, removed=0)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
