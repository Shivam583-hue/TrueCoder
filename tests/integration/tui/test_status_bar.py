"""The footer drops whole hints rather than chopping one in half."""

import unittest
from pathlib import Path

from textual.app import App, ComposeResult

from truecoder.tui import app as app_module
from truecoder.tui.widgets import _FOOTER_HINTS, StatusBar

WORKSPACE = "/home/shivam/PrgrammaingEra2/Python/TrueCoder"


class _FooterApp(App):
    CSS_PATH = str(Path(app_module.__file__).parent / "styles.tcss")

    def compose(self) -> ComposeResult:
        yield StatusBar(WORKSPACE, version="0.1.0", max_input_tokens=100000)


async def _footer(width: int) -> str:
    app = _FooterApp()
    async with app.run_test(size=(width, 6)) as pilot:
        app.query_one(StatusBar).set_conversation_active(True)
        await pilot.pause()
        for strip in app.screen._compositor.render_strips():
            if strip.text.strip():
                return strip.text
        raise AssertionError("the footer was not rendered")


def _hints_shown(footer: str) -> int:
    return sum(1 for key, action in _FOOTER_HINTS if f"{key} {action}" in footer)


class StatusBarHintTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_hint_fits_on_a_wide_terminal(self):
        footer = await _footer(160)

        self.assertEqual(_hints_shown(footer), len(_FOOTER_HINTS))

    async def test_a_hint_is_never_shown_in_part(self):
        for width in range(52, 140, 4):
            with self.subTest(width=width):
                footer = await _footer(width)

                shown = _hints_shown(footer)
                for key, action in _FOOTER_HINTS[shown:]:
                    self.assertNotIn(key, footer)
                    self.assertNotIn(action, footer)

    async def test_the_footer_never_overflows_the_terminal(self):
        for width in (52, 70, 90, 108, 120):
            with self.subTest(width=width):
                footer = await _footer(width)

                self.assertLessEqual(len(footer), width)

    async def test_a_wider_terminal_never_shows_fewer_hints(self):
        counts = [_hints_shown(await _footer(width)) for width in range(52, 160, 6)]

        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts[-1], len(_FOOTER_HINTS))


if __name__ == "__main__":
    unittest.main()
