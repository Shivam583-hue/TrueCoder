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
from truecoder.client.llm_client import LLMClient
from truecoder.providers import ApiKey, ModelInfo, Provider, SessionSettings
from truecoder.providers.store import StoredSelection
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.model_picker import ModelPickerScreen
from truecoder.tui.widgets import ChatMessage, PromptInput

CATALOG = (
    ModelInfo(
        identifier="anthropic/claude-opus-5",
        provider="test",
        context_window=200000,
    ),
    ModelInfo(identifier="openai/gpt-5", provider="test", context_window=128000),
    ModelInfo(identifier="google/gemini-3", provider="test", context_window=1000000),
)
BY_IDENTIFIER = {model.identifier: model for model in CATALOG}
MIXED = (
    ModelInfo(identifier="acme/one", provider="acme", context_window=128000),
    ModelInfo(identifier="acme/two-with-a-longer-name", provider="acme"),
    ModelInfo(identifier="brio/one", provider="brio", context_window=1000000),
)


class _Client(ScriptedLLMClient):
    def __init__(self) -> None:
        super().__init__([])
        self._settings = SessionSettings(
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
        root = Path(self._directory.name).resolve()
        self.config = root / "settings.json"
        self.addCleanup(self._directory.cleanup)
        for target, name in (
            ("truecoder.providers.store.default_settings_path", "settings.json"),
            (
                "truecoder.providers.configuration.default_providers_config_path",
                "providers.json",
            ),
        ):
            active = patch(target, return_value=root / name)
            active.start()
            self.addCleanup(active.stop)

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

                self.assertEqual(
                    app.screen.models,
                    tuple(sorted(CATALOG, key=lambda model: model.identifier)),
                )
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
                app.screen.dismiss(BY_IDENTIFIER["openai/gpt-5"])
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


class DisplayedModelTests(unittest.IsolatedAsyncioTestCase):
    def _app(self) -> TrueCoderApp:
        agent = Agent(
            llm_client=LLMClient(),
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
        )
        return TrueCoderApp(agent)

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def _isolate(self, stored: str | None):
        selection = StoredSelection(model=stored) if stored else StoredSelection()
        return (
            patch("truecoder.providers.store.load_selection", return_value=selection),
            patch(
                "truecoder.providers.configuration.load_providers", return_value=()
            ),
            patch("truecoder.providers.tokens.load_tokens", return_value={}),
        )

    async def _shown(self, *, environment: str, stored: str | None) -> str:
        app = self._app()
        patches = self._isolate(stored)

        with patch.dict(os.environ, {"MODEL": environment, "API_KEY": "sk-test"}):
            for active in patches:
                active.start()
                self.addCleanup(active.stop)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                return str(app.query_one("#composer-metadata").content)

    async def test_the_composer_shows_the_remembered_model_not_the_environment(self):
        shown = await self._shown(
            environment="cohere/north-mini-code:free",
            stored="moonshotai/kimi-k2.6",
        )

        self.assertIn("moonshotai/kimi-k2.6", shown)
        self.assertNotIn("cohere/north-mini-code:free", shown)

    async def test_the_composer_falls_back_to_the_environment_when_nothing_is_stored(
        self,
    ):
        shown = await self._shown(
            environment="cohere/north-mini-code:free",
            stored=None,
        )

        self.assertIn("cohere/north-mini-code:free", shown)

    async def test_the_composer_and_the_model_command_never_disagree(self):
        app = self._app()
        patches = self._isolate("moonshotai/kimi-k2.6")

        with patch.dict(
            os.environ,
            {"MODEL": "cohere/north-mini-code:free", "API_KEY": "sk-test"},
        ):
            for active in patches:
                active.start()
                self.addCleanup(active.stop)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                metadata = str(app.query_one("#composer-metadata").content)

                self.assertIn(app._resolved_model_name(), metadata)
                self.assertEqual(
                    app._resolved_model_name(),
                    app.agent.llm_client.settings.model,
                )


class PickerFilterTests(unittest.IsolatedAsyncioTestCase):
    def test_filtering_narrows_the_visible_models(self):
        screen = ModelPickerScreen(CATALOG, "openai/gpt-5")

        self.assertEqual(len(screen.visible_models("claude")), 1)
        self.assertEqual(len(screen.visible_models("")), 3)
        self.assertEqual(len(screen.visible_models("nothing")), 0)

    def test_filtering_by_provider_finds_that_provider(self):
        screen = ModelPickerScreen(MIXED, "openai/gpt-5")

        self.assertEqual(len(screen.visible_models("brio")), 1)
        self.assertEqual(len(screen.visible_models("acme")), 2)


class PickerLayoutTests(unittest.TestCase):
    def _lines(self, screen: ModelPickerScreen) -> list[str]:
        return [item.line() for item in screen._items(screen.models)]

    def test_one_provider_is_never_repeated_on_every_row(self):
        screen = ModelPickerScreen(CATALOG, "openai/gpt-5")

        self.assertFalse(screen.spans_providers)
        self.assertNotIn("test", "".join(self._lines(screen)))

    def test_several_providers_are_named_on_every_row(self):
        screen = ModelPickerScreen(MIXED, "acme/one")

        self.assertTrue(screen.spans_providers)
        for line in self._lines(screen):
            self.assertTrue("acme" in line or "brio" in line)

    def test_the_provider_column_starts_at_the_same_place_on_every_row(self):
        screen = ModelPickerScreen(MIXED, "acme/one")
        identifiers, _ = screen.columns()
        start = 2 + identifiers + 2

        for item in screen._items(screen.models):
            name = item.model.provider
            self.assertEqual(item.line()[start : start + len(name)], name)

    def test_the_active_row_is_the_one_from_the_active_provider(self):
        duplicated = (
            ModelInfo(identifier="shared/model", provider="acme"),
            ModelInfo(identifier="shared/model", provider="brio"),
        )
        screen = ModelPickerScreen(
            duplicated,
            "shared/model",
            active_provider="brio",
        )

        self.assertFalse(screen.is_active(duplicated[0]))
        self.assertTrue(screen.is_active(duplicated[1]))

    def test_a_million_token_window_is_not_written_as_thousands(self):
        self.assertEqual(
            ModelInfo(identifier="one", context_window=1000000).context_label,
            "1M",
        )


if __name__ == "__main__":
    unittest.main()
