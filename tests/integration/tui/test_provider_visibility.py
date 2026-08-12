"""A model identifier is not a provider, so the interface must say who serves it."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers.tui import wait_until
from tests.integration.tui.test_app import FixedTokenCounter, ScriptedLLMClient
from truecoder.agent import Agent, ContextBuilder
from truecoder.providers import ApiKey, ModelInfo, Provider, SessionSettings
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.credentials import ApiKeyScreen
from truecoder.tui.model_picker import ModelPickerScreen
from truecoder.tui.widgets import PromptInput

ROUTED = (
    ModelInfo(
        identifier="openai/gpt-5.6-sol",
        provider="default",
        context_window=400000,
    ),
    ModelInfo(
        identifier="anthropic/claude-opus-5",
        provider="default",
        context_window=1000000,
    ),
)


class _Client(ScriptedLLMClient):
    def __init__(self, settings: SessionSettings) -> None:
        super().__init__([])
        self._settings = settings


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

        for target in (
            "truecoder.providers.store.default_settings_path",
            "truecoder.providers.keys.default_keys_path",
            "truecoder.providers.tokens.default_tokens_path",
            "truecoder.providers.configuration.default_providers_config_path",
        ):
            name = (
                target.rsplit(".", 1)[-1].replace("default_", "").replace("_path", "")
            )
            active = patch(target, return_value=self.root / f"{name}.json")
            active.start()
            self.addCleanup(active.stop)

    def _app(self, credential=None) -> TrueCoderApp:
        settings = SessionSettings(
            provider=Provider(name="default", base_url="https://openrouter.ai/api/v1"),
            credential=credential,
            model="openai/gpt-5.6-sol",
        )
        agent = Agent(
            llm_client=_Client(settings),
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
        )
        return TrueCoderApp(agent)

    async def _open_picker(self, app, pilot):
        app.query_one(PromptInput).text = "/models"
        await pilot.press("enter")
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, ModelPickerScreen),
            description="the model picker",
        )


class ProviderVisibilityTests(_Base):
    async def test_an_unbranded_provider_is_never_named_to_the_user(self):
        app = self._app(ApiKey("sk-or-1"))

        with (
            patch.dict(os.environ, {"MODEL": "openai/gpt-5.6-sol"}),
            patch("truecoder.providers.catalog.load_models", return_value=ROUTED),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._open_picker(app, pilot)

                self.assertEqual(app.screen.served_by(), "")

                rendered = "".join(
                    "".join(segment.text for segment in strip)
                    for strip in app.screen._compositor.render_strips()
                )
                self.assertNotIn("Served by", rendered)
                self.assertNotIn("openrouter", rendered)

                app.screen.dismiss(None)
                await pilot.pause()

    async def test_a_display_name_is_what_the_user_sees(self):
        provider = Provider(
            name="default",
            base_url="https://openrouter.ai/api/v1",
            display_name="TrueCoder Cloud",
        )

        self.assertTrue(provider.is_named)
        self.assertEqual(provider.label, "TrueCoder Cloud")

    async def test_an_unbranded_provider_is_not_named(self):
        provider = Provider(name="default", base_url="https://openrouter.ai/api/v1")

        self.assertFalse(provider.is_named)

    async def test_a_named_provider_keeps_its_name(self):
        self.assertEqual(
            Provider(name="acme", base_url="https://x.invalid").label, "acme"
        )

    async def test_the_upstream_host_never_reaches_the_notice(self):
        app = self._app(ApiKey("sk-or-1"))
        notices: list[str] = []

        with (
            patch.dict(os.environ, {"MODEL": "openai/gpt-5.6-sol"}),
            patch("truecoder.providers.catalog.load_models", return_value=ROUTED),
            patch.object(
                TrueCoderApp,
                "notify",
                lambda self, message, **kwargs: notices.append(str(message)),
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._open_picker(app, pilot)
                app.screen.dismiss(ROUTED[0])
                await pilot.pause()

                joined = " ".join(notices)
                self.assertIn("Now answering with openai/gpt-5.6-sol", joined)
                self.assertNotIn("openrouter", joined)
                self.assertNotIn("via", joined)


class KeyOnlyProviderTests(_Base):
    async def test_the_prompt_explains_why_there_is_no_sign_in(self):
        app = self._app()

        with patch.dict(os.environ, {"MODEL": "openai/gpt-5.6-sol"}):
            async with app.run_test(size=(120, 40)) as pilot:
                app.query_one(PromptInput).text = "/login"
                await pilot.press("enter")
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ApiKeyScreen),
                    description="the api key prompt",
                )

                self.assertFalse(app.screen.browser_sign_in)

                rendered = "".join(
                    "".join(segment.text for segment in strip)
                    for strip in app.screen._compositor.render_strips()
                )
                self.assertIn("no browser sign-in configured", rendered)
                self.assertIn("providers.json", rendered)

                app.screen.dismiss(None)
                await pilot.pause()

    async def test_a_provider_with_a_sign_in_never_shows_that_note(self):
        screen = ApiKeyScreen("acme", "acme/one", browser_sign_in=True)
        app = self._app()

        with patch.dict(os.environ, {"MODEL": "openai/gpt-5.6-sol"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await app.push_screen(screen)
                await pilot.pause()

                rendered = "".join(
                    "".join(segment.text for segment in strip)
                    for strip in app.screen._compositor.render_strips()
                )
                self.assertNotIn("no browser sign-in configured", rendered)


if __name__ == "__main__":
    unittest.main()
