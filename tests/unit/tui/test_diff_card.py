from __future__ import annotations

import unittest

from truecoder.mutation import FileDiff, MutationKind, build_file_diff
from truecoder.tui.widgets import ToolCallCard


def _diff(
    before: str,
    after: str,
    *,
    kind: MutationKind = "edit",
    **kwargs,
) -> FileDiff:
    return build_file_diff("src/app.py", before, after, kind=kind, **kwargs)


def _card(mutation: FileDiff | None) -> ToolCallCard:
    return ToolCallCard(
        "call_1",
        "edit_file",
        {"path": "src/app.py"},
        state="awaiting-approval",
        mutation=mutation,
    )


class DiffCardConstructionTests(unittest.TestCase):
    def test_a_card_without_a_mutation_renders_nothing(self):
        self.assertEqual(_card(None)._diff_text().plain, "")

    def test_a_non_diff_mutation_is_rejected(self):
        with self.assertRaises(TypeError):
            ToolCallCard("call_1", "edit_file", {}, mutation="+1 -1")  # type: ignore[arg-type]

    def test_a_non_diff_mutation_is_rejected_when_awaiting_approval(self):
        card = _card(None)

        with self.assertRaises(TypeError):
            card.set_awaiting_approval({}, mutation="+1 -1")  # type: ignore[arg-type]


class DiffCardRenderingTests(unittest.TestCase):
    def test_added_and_removed_lines_carry_their_markers(self):
        card = _card(_diff("one\ntwo\n", "one\nTWO\n"))

        lines = card._diff_text().plain.splitlines()

        self.assertTrue(lines[0].startswith("@@"))
        self.assertIn("  one", lines[1])
        self.assertTrue(lines[2].startswith("- "))
        self.assertIn("two", lines[2])
        self.assertTrue(lines[3].startswith("+ "))
        self.assertIn("TWO", lines[3])

    def test_a_removed_line_shows_the_original_number(self):
        card = _card(_diff("one\ntwo\n", "one\n"))

        removed = [
            line
            for line in card._diff_text().plain.splitlines()
            if line.startswith("- ")
        ]

        self.assertEqual(removed, ["-    2  two"])

    def test_an_added_line_shows_the_resulting_number(self):
        card = _card(_diff("one\n", "one\ntwo\n"))

        added = [
            line
            for line in card._diff_text().plain.splitlines()
            if line.startswith("+ ")
        ]

        self.assertEqual(added, ["+    2  two"])

    def test_each_line_kind_is_styled_distinctly(self):
        card = _card(_diff("one\ntwo\n", "one\nTWO\n"))

        styles = {str(span.style) for span in card._diff_text().spans}

        self.assertGreaterEqual(len(styles), 3)

    def test_a_truncated_diff_says_so(self):
        after = "\n".join(f"line {number}" for number in range(500))
        card = _card(_diff("", after, kind="create", max_lines=10))

        self.assertIn("diff truncated", card._diff_text().plain)

    def test_a_trailing_newline_change_is_shown(self):
        card = _card(_diff("one\n", "one"))

        self.assertIn("trailing newline changed", card._diff_text().plain)

    def test_a_line_ending_change_is_shown_rather_than_an_empty_diff(self):
        card = _card(_diff("one\r\ntwo\r\n", "one\ntwo\n", kind="replace"))

        rendered = card._diff_text().plain

        self.assertIn("line endings changed", rendered)
        self.assertNotIn("trailing newline changed", rendered)

    def test_separate_hunks_are_separated_by_a_blank_line(self):
        before = "\n".join(str(number) for number in range(1, 41))
        after = before.replace("2\n", "TWO\n", 1).replace("38\n", "THIRTY8\n", 1)
        card = _card(_diff(before, after))

        headers = [
            index
            for index, line in enumerate(card._diff_text().plain.splitlines())
            if line.startswith("@@")
        ]

        self.assertEqual(len(headers), 2)


class DiffCardSummaryTests(unittest.TestCase):
    def test_the_summary_reports_the_diff_stat(self):
        card = _card(_diff("one\ntwo\n", "one\nTWO\nthree\n"))

        self.assertEqual(card._parameter_summary, "+2  -1")

    def test_the_summary_falls_back_to_arguments_without_a_diff(self):
        card = ToolCallCard(
            "call_1",
            "edit_file",
            {"path": "src/app.py", "replace_all": False},
            state="awaiting-approval",
        )

        self.assertIn("replace all", card._parameter_summary)

    def test_a_card_with_a_diff_is_marked_for_the_stylesheet(self):
        card = _card(None)

        card.set_awaiting_approval({"path": "a.py"}, mutation=_diff("a\n", "b\n"))

        self.assertIn("has-diff", card.classes)

    def test_a_card_without_a_diff_is_not_marked(self):
        card = _card(None)

        card.set_awaiting_approval({"path": "a.py"})

        self.assertNotIn("has-diff", card.classes)


if __name__ == "__main__":
    unittest.main()
