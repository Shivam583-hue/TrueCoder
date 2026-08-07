from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from truecoder.tui.widgets import ToolCallCard

TUI_DIRECTORY = Path(__file__).resolve().parents[3] / "src" / "truecoder" / "tui"

BRACKETED_ERROR = json.dumps(
    {
        "status": "error",
        "error": (
            "Arguments for tool 'read_file' failed validation: 1 validation "
            "error for ReadFileArguments\nstart_line\n  Input should be greater "
            "than or equal to 1 [type=greater_than_equal, input_value=0, "
            "input_type=int]"
        ),
        "error_code": "invalid_arguments",
    }
)


def _unmarked_statics(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", "") not in {"Static", "Label"}:
            continue
        if any(keyword.arg == "markup" for keyword in node.keywords):
            continue
        if not node.args:
            continue
        if isinstance(node.args[0], ast.Constant):
            continue
        found.append((node.lineno, ast.unparse(node.args[0])))

    return found


class MarkupSafetyTests(unittest.TestCase):
    def test_no_widget_renders_computed_text_as_markup(self):
        offenders: list[str] = []
        for path in sorted(TUI_DIRECTORY.glob("*.py")):
            for line, source in _unmarked_statics(path):
                offenders.append(f"{path.name}:{line} {source}")

        self.assertEqual(
            offenders,
            [],
            "these widgets render computed text as markup; pass markup=False",
        )


class ToolCardMarkupTests(unittest.TestCase):
    def _card(self) -> ToolCallCard:
        return ToolCallCard(
            "call_1",
            "read_file",
            '{"path": "README.md"}',
            state="running",
        )

    def test_a_bracketed_error_is_kept_verbatim(self):
        card = self._card()

        card.finish("error", BRACKETED_ERROR)

        self.assertIn("[type=greater_than_equal", card._details_text())

    def test_bracketed_arguments_are_kept_verbatim(self):
        card = ToolCallCard(
            "call_1",
            "grep",
            json.dumps({"pattern": "[a-z]+", "path": "."}),
            state="running",
        )

        self.assertIn("[a-z]+", card._details_text())


if __name__ == "__main__":
    unittest.main()
