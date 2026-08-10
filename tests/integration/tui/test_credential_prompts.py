"""Choosing a model that cannot answer must ask for what it is missing."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers.tui import wait_until
from tests.integration.tui.test_app import FixedTokenCounter, ScriptedLLMClient
from truecoder.agent import Agent, ContextBuilder
from truecoder.providers import ApiKey, ModelInfo, Provider, SessionSettings
from truecoder.providers.oauth import OAuthClient, OAuthError, OAuthToken
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.credentials import (
    BROWSER_OPENED,
    BROWSER_REFUSED,
    COPIED_MESSAGE,
    ApiKeyScreen,
    AuthorisationScreen,
)
from truecoder.tui.model_picker import ModelPickerScreen
from truecoder.tui.widgets import PromptInput

CATALOG = (ModelInfo(identifier="openai/gpt-5", context_window=128000),)

OAUTH = OAuthClient(
    client_id="client-123",
    authorize_url="https://provider.invalid/oauth/authorize",
    token_url="https://provider.invalid/oauth/token",
)


class _Client(ScriptedLLMClient):
    def __init__(self, settings: SessionSettings) -> None:
        super().__init__([])
        self._settings = settings


def _settings(*, credential, oauth=None) -> SessionSettings:
    return SessionSettings(
        provider=Provider(
            name="acme",
            base_url="https://api.acme.invalid/v1",
            oauth=oauth,
        ),
        credential=credential,
        model="acme/starter",
    )


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

        for target in (
            "truecoder.providers.store.default_settings_path",
            "truecoder.providers.keys.default_keys_path",
            "truecoder.providers.tokens.default_tokens_path",
        ):
            name = (
                target.rsplit(".", 1)[-1].replace("default_", "").replace("_path", "")
            )
            active = patch(target, return_value=self.root / f"{name}.json")
            active.start()
            self.addCleanup(active.stop)

    def _app(self, settings: SessionSettings) -> TrueCoderApp:
        agent = Agent(
            llm_client=_Client(settings),
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
        )
        return TrueCoderApp(agent)

    async def _pick(self, app, pilot, model: str = "openai/gpt-5") -> None:
        app.query_one(PromptInput).text = "/models"
        await pilot.press("enter")
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, ModelPickerScreen),
            description="the model picker",
        )
        app.screen.dismiss(model)
        await pilot.pause()


class ApiKeyPromptTests(_Base):
    async def test_choosing_a_model_without_a_key_asks_for_one(self):
        app = self._app(_settings(credential=None))

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ApiKeyScreen),
                    description="the api key prompt",
                )

                self.assertEqual(app.screen.provider, "acme")

    async def test_a_typed_key_is_used_and_remembered(self):
        app = self._app(_settings(credential=None))

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ApiKeyScreen),
                    description="the api key prompt",
                )
                app.screen.dismiss("sk-typed")
                await wait_until(
                    pilot,
                    lambda: app.agent.llm_client.settings.credential is not None,
                    description="the key to be adopted",
                )

                self.assertEqual(
                    app.agent.llm_client.settings.credential,
                    ApiKey("sk-typed"),
                )
                self.assertIn("sk-typed", (self.root / "keys.json").read_text())

    async def test_a_model_with_a_usable_key_asks_for_nothing(self):
        app = self._app(_settings(credential=ApiKey("sk-existing")))

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await pilot.pause()

                self.assertNotIsInstance(app.screen, ApiKeyScreen)

    async def test_skipping_the_prompt_leaves_the_credential_alone(self):
        app = self._app(_settings(credential=None))

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ApiKeyScreen),
                    description="the api key prompt",
                )
                app.screen.dismiss(None)
                await pilot.pause()

                self.assertIsNone(app.agent.llm_client.settings.credential)
                self.assertFalse((self.root / "keys.json").exists())

    async def test_the_key_is_never_echoed_to_the_screen(self):
        screen = ApiKeyScreen("acme", "acme/starter")
        app = self._app(_settings(credential=None))

        with patch.dict(os.environ, {"MODEL": "acme/starter"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await app.push_screen(screen)
                await pilot.pause()
                await pilot.press(*"sk-secret")
                await pilot.pause()

                rendered = "".join(
                    "".join(segment.text for segment in strip)
                    for strip in app.screen._compositor.render_strips()
                )

                self.assertNotIn("sk-secret", rendered)
                self.assertIn("•", rendered)


class LoginCommandTests(_Base):
    async def _run(self, app, pilot, command: str) -> None:
        app.query_one(PromptInput).text = command
        await pilot.press("enter")
        await pilot.pause()

    async def test_login_asks_for_a_key_when_the_provider_has_no_oauth(self):
        app = self._app(_settings(credential=None))

        with patch.dict(os.environ, {"MODEL": "acme/starter"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._run(app, pilot, "/login")
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ApiKeyScreen),
                    description="the api key prompt",
                )

                self.assertEqual(app.screen.provider, "acme")
                app.screen.dismiss(None)
                await pilot.pause()

    async def test_logout_forgets_a_stored_key_as_well_as_a_token(self):
        from truecoder.providers.keys import load_keys, store_key

        store_key("acme", ApiKey("sk-stored"), self.root / "keys.json")
        app = self._app(_settings(credential=ApiKey("sk-stored")))
        notices: list[str] = []

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch.object(
                TrueCoderApp,
                "notify",
                lambda self, message, **kwargs: notices.append(str(message)),
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._run(app, pilot, "/logout")

                self.assertEqual(load_keys(self.root / "keys.json"), {})
                self.assertIsNone(app.agent.llm_client.settings.credential)
                self.assertTrue(any("API key" in note for note in notices))

    async def test_logout_with_nothing_stored_says_so(self):
        app = self._app(_settings(credential=None))
        notices: list[str] = []

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch.object(
                TrueCoderApp,
                "notify",
                lambda self, message, **kwargs: notices.append(str(message)),
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._run(app, pilot, "/logout")

                self.assertTrue(any("Nothing stored" in note for note in notices))


class AuthorisationPromptTests(_Base):
    def _pending(self, url: str = "https://provider.invalid/oauth/authorize?x=1"):
        class _Pending:
            def __init__(self) -> None:
                self.url = url
                self.closed = False
                self.token = asyncio.get_event_loop().create_future()

            async def wait(self, *, timeout: float = 300.0):
                return await self.token

            async def close(self) -> None:
                self.closed = True

        return _Pending()

    async def test_choosing_an_oauth_model_shows_the_link_and_opens_a_browser(self):
        app = self._app(_settings(credential=None, oauth=OAUTH))
        pending = self._pending()
        opened: list[str] = []

        async def begin(client, *, provider=""):
            return pending

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
            patch("truecoder.providers.login.begin_login", side_effect=begin),
            patch(
                "truecoder.providers.login.open_in_browser",
                side_effect=lambda target: (opened.append(target), True)[1],
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, AuthorisationScreen),
                    description="the authorisation screen",
                )

                self.assertEqual(app.screen.url, pending.url)
                self.assertEqual(opened, [pending.url])
                self.assertTrue(app.screen.browser_opened)

                pending.token.set_result(
                    OAuthToken(access_token="at-1", provider="acme")
                )
                await wait_until(
                    pilot,
                    lambda: app.agent.llm_client.settings.credential is not None,
                    description="the token to be adopted",
                )

    async def test_the_link_is_shown_even_when_no_browser_opens(self):
        app = self._app(_settings(credential=None, oauth=OAUTH))
        pending = self._pending()

        async def begin(client, *, provider=""):
            return pending

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
            patch("truecoder.providers.login.begin_login", side_effect=begin),
            patch("truecoder.providers.login.open_in_browser", return_value=False),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, AuthorisationScreen),
                    description="the authorisation screen",
                )

                rendered = "".join(
                    "".join(segment.text for segment in strip)
                    for strip in app.screen._compositor.render_strips()
                )

                self.assertFalse(app.screen.browser_opened)
                self.assertIn(BROWSER_REFUSED[:24], rendered)

                app.screen.dismiss(False)
                await pilot.pause()

    async def test_the_link_can_be_copied(self):
        app = self._app(_settings(credential=None, oauth=OAUTH))
        pending = self._pending()

        async def begin(client, *, provider=""):
            return pending

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
            patch("truecoder.providers.login.begin_login", side_effect=begin),
            patch("truecoder.providers.login.open_in_browser", return_value=True),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, AuthorisationScreen),
                    description="the authorisation screen",
                )

                app.screen.copy_link()
                await pilot.pause()

                self.assertEqual(app.clipboard, pending.url)
                status = app.screen.query_one("#authorisation-status")
                self.assertIn(COPIED_MESSAGE, str(status.content))

                app.screen.dismiss(False)
                await pilot.pause()

    async def test_reopening_the_browser_reports_what_happened(self):
        screen = AuthorisationScreen("acme", "https://x.invalid/a", browser_opened=True)
        app = self._app(_settings(credential=None, oauth=OAUTH))

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.login.open_in_browser", return_value=False),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await app.push_screen(screen)
                await pilot.pause()

                screen.open_again()
                await pilot.pause()

                self.assertIn(
                    BROWSER_REFUSED,
                    str(screen.query_one("#authorisation-status").content),
                )

        self.assertTrue(BROWSER_OPENED)

    async def test_the_actions_stay_on_screen_even_with_a_long_url(self):
        long_url = "https://provider.invalid/oauth/authorize?" + "&".join(
            f"parameter{index}=value{index}" for index in range(20)
        )
        screen = AuthorisationScreen("acme", long_url, browser_opened=True)
        app = self._app(_settings(credential=None, oauth=OAUTH))

        with patch.dict(os.environ, {"MODEL": "acme/starter"}):
            async with app.run_test(size=(58, 28)) as pilot:
                await app.push_screen(screen)
                await pilot.pause()
                await wait_until(
                    pilot,
                    lambda: screen.query_one("#copy-link").region.height > 0,
                    description="the copy button to be laid out",
                )

                rendered = "".join(
                    "".join(segment.text for segment in strip)
                    for strip in app.screen._compositor.render_strips()
                )

                self.assertIn("Copy link", rendered)
                self.assertIn("esc cancel", rendered)

    async def test_the_copy_shortcut_works_without_the_mouse(self):
        screen = AuthorisationScreen("acme", "https://x.invalid/a", browser_opened=True)
        app = self._app(_settings(credential=None, oauth=OAUTH))

        with patch.dict(os.environ, {"MODEL": "acme/starter"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await app.push_screen(screen)
                await pilot.pause()
                await pilot.press("c")
                await pilot.pause()

                self.assertEqual(app.clipboard, "https://x.invalid/a")

    async def test_cancelling_closes_the_callback_server(self):
        app = self._app(_settings(credential=None, oauth=OAUTH))
        pending = self._pending()

        async def begin(client, *, provider=""):
            return pending

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
            patch("truecoder.providers.login.begin_login", side_effect=begin),
            patch("truecoder.providers.login.open_in_browser", return_value=True),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, AuthorisationScreen),
                    description="the authorisation screen",
                )

                app.screen.dismiss(False)
                await wait_until(
                    pilot,
                    lambda: pending.closed,
                    description="the callback server to be closed",
                )

                self.assertIsNone(app.agent.llm_client.settings.credential)

    async def test_a_provider_without_an_oauth_client_says_so(self):
        app = self._app(_settings(credential=None))
        notices: list[str] = []

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch.object(
                TrueCoderApp,
                "notify",
                lambda self, message, **kwargs: notices.append(str(message)),
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await app._authorise_provider()
                await pilot.pause()

                self.assertTrue(any("providers.json" in note for note in notices))

    async def test_a_failed_start_is_reported_not_raised(self):
        app = self._app(_settings(credential=None, oauth=OAUTH))
        notices: list[str] = []

        async def begin(client, *, provider=""):
            raise OAuthError("the provider refused")

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.login.begin_login", side_effect=begin),
            patch.object(
                TrueCoderApp,
                "notify",
                lambda self, message, **kwargs: notices.append(str(message)),
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await app._authorise_provider()
                await pilot.pause()

                self.assertTrue(any("the provider refused" in note for note in notices))


if __name__ == "__main__":
    unittest.main()
