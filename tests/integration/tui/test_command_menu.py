"""Typing a slash shows what is available and narrows it as the name grows."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests.helpers.tui import wait_until
from tests.integration.tui.test_app import FixedTokenCounter, ScriptedLLMClient
from truecoder.agent import Agent, ContextBuilder
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.commands import COMMANDS
from truecoder.tui.widgets import ChatMessage, CommandMenu, PromptInput


def _app() -> TrueCoderApp:
    agent = Agent(
        llm_client=ScriptedLLMClient([]),
        context_builder=ContextBuilder(
            system_prompt="test system",
            max_input_tokens=1000,
            token_counter=FixedTokenCounter(),
        ),
    )
    return TrueCoderApp(agent)


def _offered(app: TrueCoderApp) -> list[str]:
    return list(app.query_one(CommandMenu).offered)


class CommandMenuTests(unittest.IsolatedAsyncioTestCase):
    async def _type(self, pilot, text: str) -> None:
        await pilot.press(*text)
        await pilot.pause()

    async def test_a_bare_slash_offers_every_command(self):
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._type(pilot, "/")

                self.assertEqual(
                    _offered(app),
                    [command.name for command in COMMANDS],
                )

    async def test_every_offered_command_has_room_to_be_seen(self):
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._type(pilot, "/")
                menu = app.query_one(CommandMenu)
                await wait_until(
                    pilot,
                    lambda: menu.region.height > 0,
                    description="the command menu to be laid out",
                )

                heights = [row.region.height for row in menu.query(".command-menu-row")]

                self.assertGreaterEqual(menu.region.height, len(COMMANDS))
                self.assertEqual(len(heights), len(COMMANDS))
                self.assertEqual(heights, [1] * len(COMMANDS))

    async def test_a_summary_too_wide_to_fit_is_ellipsised_not_chopped(self):
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(40, 24)) as pilot:
                await self._type(pilot, "/")
                menu = app.query_one(CommandMenu)
                await wait_until(
                    pilot,
                    lambda: menu.region.height > 0,
                    description="the command menu to be laid out",
                )

                rendered = [
                    "".join(segment.text for segment in strip)
                    for strip in app.screen._compositor.render_strips()
                ]
                logout = next(line for line in rendered if "/logout" in line)

                self.assertIn("…", logout)
                self.assertNotIn("Forget the stored authorisation", logout)

    async def test_the_menu_is_hidden_until_a_slash_is_typed(self):
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                self.assertEqual(_offered(app), [])

                await self._type(pilot, "fix the build")

                self.assertEqual(_offered(app), [])

    async def test_a_letter_narrows_the_offer(self):
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._type(pilot, "/q")

                self.assertEqual(_offered(app), ["quit"])

    async def test_the_offer_keeps_narrowing_as_the_name_grows(self):
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._type(pilot, "/m")
                self.assertEqual(_offered(app), ["models", "model"])

                await self._type(pilot, "o")
                self.assertEqual(_offered(app), ["models", "model"])

                await self._type(pilot, "dels")
                self.assertEqual(_offered(app), ["models"])

    async def test_deleting_widens_the_offer_again(self):
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._type(pilot, "/models")
                self.assertEqual(_offered(app), ["models"])

                await pilot.press("backspace")
                await pilot.pause()

                self.assertEqual(_offered(app), ["models", "model"])

    async def test_an_unknown_prefix_offers_nothing(self):
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._type(pilot, "/zzz")

                self.assertEqual(_offered(app), [])

    async def test_the_menu_closes_once_an_argument_is_being_typed(self):
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._type(pilot, "/models")
                self.assertEqual(_offered(app), ["models"])

                await pilot.press("space")
                await pilot.pause()

                self.assertEqual(_offered(app), [])

    async def test_running_a_command_clears_the_menu(self):
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._type(pilot, "/help")
                await pilot.press("enter")
                await pilot.pause()

                self.assertEqual(_offered(app), [])
                self.assertEqual(app.query_one(PromptInput).text, "")


class TabCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def _complete(self, typed: str) -> str:
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press(*typed)
                await pilot.pause()
                await pilot.press("tab")
                await pilot.pause()

                return app.query_one(PromptInput).text

    async def test_a_single_match_completes_fully(self):
        self.assertEqual(await self._complete("/q"), "/quit")

    async def test_several_matches_complete_to_what_they_share(self):
        self.assertEqual(await self._complete("/m"), "/model")
        self.assertEqual(await self._complete("/l"), "/log")

    async def test_an_ordinary_prompt_is_left_alone(self):
        self.assertEqual(await self._complete("fix the build"), "fix the build")

    async def test_an_unknown_prefix_is_left_alone(self):
        self.assertEqual(await self._complete("/zzz"), "/zzz")

    async def test_a_completed_command_can_be_submitted(self):
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("/", "h")
                await pilot.pause()
                await pilot.press("tab")
                await pilot.pause()

                self.assertEqual(app.query_one(PromptInput).text, "/help")

                await pilot.press("enter")
                await pilot.pause()

                self.assertEqual(list(app.query(ChatMessage)), [])
                self.assertEqual(app.agent.llm_client.calls, [])

    async def test_the_cursor_lands_after_the_completed_name(self):
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("/", "q")
                await pilot.pause()
                await pilot.press("tab")
                await pilot.pause()
                await pilot.press("!")
                await pilot.pause()

                self.assertEqual(app.query_one(PromptInput).text, "/quit!")


class QuitCommandTests(unittest.IsolatedAsyncioTestCase):
    async def _exit_state(self, *keys: str) -> tuple[object, object]:
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press(*keys)
                await wait_until(
                    pilot,
                    lambda: app.return_code is not None,
                    description="the app to be asked to exit",
                )

                return app.return_code, app.return_value

    async def test_the_quit_command_exits_exactly_like_the_shortcut(self):
        by_command = await self._exit_state(*"/quit", "enter")
        by_shortcut = await self._exit_state("ctrl+q")

        self.assertEqual(by_command, by_shortcut)
        self.assertEqual(by_command, (0, None))

    async def test_quitting_never_reaches_the_model(self):
        app = _app()

        with patch.dict(os.environ, {"MODEL": "test/model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press(*"/quit", "enter")
                await wait_until(
                    pilot,
                    lambda: app.return_code is not None,
                    description="the app to be asked to exit",
                )

                self.assertEqual(app.agent.llm_client.calls, [])


if __name__ == "__main__":
    unittest.main()
