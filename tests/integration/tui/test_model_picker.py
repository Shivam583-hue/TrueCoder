"""Typing a command must change the agent, not become a prompt for it."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers.tui import wait_until
from tests.integration.tui.test_app import (
    FixedTokenCounter,
    ScriptedLLMClient,
)
from truecoder.agent import Agent, ContextBuilder
from truecoder.providers import ApiKey, ModelInfo, Provider, SessionSettings
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.model_picker import ModelPickerScreen
from truecoder.tui.widgets import ChatMessage, PromptInput

CATALOG = (
    ModelInfo(identifier="anthropic/claude-opus-5", context_window=200000),
    ModelInfo(identifier="openai/gpt-5", context_window=128000),
    ModelInfo(identifier="google/gemini-3", context_window=1000000),
)


class _Client(ScriptedLLMClient):
    def __init__(self) -> None:
        super().__init__([])
        self.settings = SessionSettings(
            provider=Provider(name="test", base_url="https://x.invalid/v1"),
            credential=ApiKey("sk-test"),
            model="anthropic/claude-opus-5",
        )


class ModelCommandTests(unittest.IsolatedAsyncioTestCase):
    def _app(self) -> TrueCoderApp:
        agent = Agent(
            llm_client=_Client(),
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
        )
        return TrueCoderApp(agent)

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.config = Path(self._directory.name).resolve() / "settings.json"
        self.addCleanup(self._directory.cleanup)
        saver = patch(
            "truecoder.providers.store.default_settings_path",
            return_value=self.config,
        )
        saver.start()
        self.addCleanup(saver.stop)

    async def _type(self, app: TrueCoderApp, pilot, text: str) -> None:
        app.query_one(PromptInput).text = text
        await pilot.press("enter")
        await pilot.pause()

    async def test_a_command_never_reaches_the_model(self):
        app = self._app()

        with patch.dict(os.environ, {"MODEL": "anthropic/claude-opus-5"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._type(app, pilot, "/help")

                self.assertEqual(list(app.query(ChatMessage)), [])
                self.assertEqual(app.agent.llm_client.calls, [])

    async def test_an_unknown_command_is_reported_not_sent(self):
        app = self._app()

        with patch.dict(os.environ, {"MODEL": "anthropic/claude-opus-5"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._type(app, pilot, "/nonsense")

                self.assertEqual(list(app.query(ChatMessage)), [])
                self.assertEqual(app.agent.llm_client.calls, [])

    async def test_an_ordinary_prompt_still_reaches_the_model(self):
        app = self._app()

        with patch.dict(os.environ, {"MODEL": "anthropic/claude-opus-5"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._type(app, pilot, "hello there")
                await wait_until(
                    pilot,
                    lambda: bool(app.query(ChatMessage)),
                    description="the prompt to reach the transcript",
                )

                self.assertTrue(app.agent.llm_client.calls)

    async def test_the_picker_opens_and_lists_the_catalog(self):
        app = self._app()

        with (
            patch.dict(os.environ, {"MODEL": "anthropic/claude-opus-5"}),
            patch(
                "truecoder.providers.catalog.load_models",
                return_value=CATALOG,
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._type(app, pilot, "/models")
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ModelPickerScreen),
                    description="the model picker",
                )

                self.assertEqual(app.screen.models, CATALOG)
                self.assertEqual(app.screen.active_model, "anthropic/claude-opus-5")

    async def test_choosing_a_model_changes_what_the_agent_will_use(self):
        app = self._app()

        with (
            patch.dict(os.environ, {"MODEL": "anthropic/claude-opus-5"}),
            patch(
                "truecoder.providers.catalog.load_models",
                return_value=CATALOG,
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._type(app, pilot, "/models")
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ModelPickerScreen),
                    description="the model picker",
                )
                app.screen.dismiss("openai/gpt-5")
                await pilot.pause()

                self.assertEqual(
                    app.agent.llm_client.settings.model,
                    "openai/gpt-5",
                )
                self.assertEqual(app._model_name, "openai/gpt-5")
                self.assertIn("openai/gpt-5", self.config.read_text())

    async def test_an_unreachable_provider_explains_itself(self):
        app = self._app()

        from truecoder.providers.catalog import CatalogError

        with (
            patch.dict(os.environ, {"MODEL": "anthropic/claude-opus-5"}),
            patch(
                "truecoder.providers.catalog.load_models",
                side_effect=CatalogError("the provider returned 401"),
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._type(app, pilot, "/models")
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ModelPickerScreen),
                    description="the model picker",
                )

                self.assertIn("401", str(app.screen.unavailable_reason))


class PickerFilterTests(unittest.IsolatedAsyncioTestCase):
    def test_filtering_narrows_the_visible_models(self):
        screen = ModelPickerScreen(CATALOG, "openai/gpt-5")

        self.assertEqual(len(screen.visible_models("claude")), 1)
        self.assertEqual(len(screen.visible_models("")), 3)
        self.assertEqual(len(screen.visible_models("nothing")), 0)


if __name__ == "__main__":
    unittest.main()
