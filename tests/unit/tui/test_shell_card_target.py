"""A shell call that never started must still say what was attempted."""

from __future__ import annotations

import json
import unittest

from truecoder.tui.widgets import MAX_TARGET_CHARACTERS, ToolCallCard

FAILURE = json.dumps(
    {
        "status": "error",
        "error": "Arguments for tool 'shell' failed validation",
        "error_code": "invalid_arguments",
    }
)


def _card(arguments: dict, *, tool_name: str = "shell") -> ToolCallCard:
    card = ToolCallCard("call_1", tool_name, json.dumps(arguments), state="running")
    card.finish("error", FAILURE)
    return card


class ShellCardTargetTests(unittest.TestCase):
    def test_an_exec_command_is_shown(self):
        card = _card({"mode": "exec", "argv": ["python", "-m", "pytest", "tests/"]})

        self.assertIn("python -m pytest tests/", card._headline())

    def test_a_shell_script_is_shown(self):
        card = _card({"mode": "shell", "script": "pytest -q | tee report.txt"})

        self.assertIn("pytest -q | tee report.txt", card._headline())

    def test_a_failed_call_is_never_left_without_its_command(self):
        card = _card({"mode": "exec", "argv": ["ls", "-la"]})

        self.assertNotEqual(card._headline().split(" · ")[0].strip(), "Failed shell")

    def test_a_long_command_is_bounded(self):
        card = _card(
            {"mode": "exec", "argv": ["python"] + [f"--flag-{n}" for n in range(40)]}
        )

        headline = card._headline().split(" · ")[0]
        self.assertLessEqual(
            len(headline), len("Failed shell ") + MAX_TARGET_CHARACTERS
        )
        self.assertTrue(headline.endswith("…"))

    def test_newlines_in_a_script_never_break_the_headline(self):
        card = _card({"mode": "shell", "script": "set -e\npytest -q\necho done"})

        headline = card._headline().split(" · ")[0]
        self.assertNotIn("\n", headline)
        self.assertIn("set -e pytest -q echo done", headline)

    def test_an_ordinary_path_target_is_unchanged(self):
        card = _card({"path": "README.md"}, tool_name="read_file")

        self.assertIn("README.md", card._headline())


if __name__ == "__main__":
    unittest.main()
