"""A risky tool that succeeded must not read as a failure."""

import unittest
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Static

from truecoder.tui import app as app_module
from truecoder.tui.widgets import ToolCallCard


class _CardApp(App):
    CSS_PATH = str(Path(app_module.__file__).parent / "styles.tcss")

    def __init__(self, tool_name: str, state: str) -> None:
        self._tool_name = tool_name
        self._state = state
        super().__init__()

    def compose(self) -> ComposeResult:
        yield ToolCallCard(
            "call_1",
            self._tool_name,
            '{"command": "python -m unittest"}',
            state=self._state,
        )


async def _label_colour(tool_name: str, state: str) -> str:
    app = _CardApp(tool_name, state)
    async with app.run_test(size=(120, 12)) as pilot:
        await pilot.pause()
        label = app.query_one(".tool-state-label", Static)
        return label.styles.color.hex


class RiskyCardColourTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_completed_shell_call_is_not_coloured_as_a_failure(self):
        completed = await _label_colour("shell", "completed")
        failed = await _label_colour("shell", "failed")

        self.assertNotEqual(completed, failed)

    async def test_a_completed_shell_call_matches_a_completed_safe_call(self):
        risky = await _label_colour("shell", "completed")
        safe = await _label_colour("read_file", "completed")

        self.assertEqual(risky, safe)

    async def test_a_failed_shell_call_is_still_coloured_as_a_failure(self):
        risky = await _label_colour("shell", "failed")
        safe = await _label_colour("read_file", "failed")

        self.assertEqual(risky, safe)

    async def test_an_awaiting_shell_call_is_still_marked_risky(self):
        risky = await _label_colour("shell", "awaiting-approval")
        safe = await _label_colour("read_file", "awaiting-approval")

        self.assertNotEqual(risky, safe)


if __name__ == "__main__":
    unittest.main()
