"""Choosing a model that cannot answer must ask for what it is missing."""

from __future__ import annotations

import asyncio
import json
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
    DEVICE_CHOICE,
    KEY_CHOICE,
    OAUTH_CHOICE,
    ApiKeyScreen,
    AuthorisationScreen,
    CredentialChoiceScreen,
    DeviceCodeScreen,
)
from truecoder.tui.model_picker import ModelPickerScreen
from truecoder.tui.widgets import PromptInput, SystemNote

CATALOG = (
    ModelInfo(identifier="openai/gpt-5", provider="acme", context_window=128000),
)

OAUTH = OAuthClient(
    client_id="client-123",
    authorize_url="https://provider.invalid/oauth/authorize",
    token_url="https://provider.invalid/oauth/token",
)


class _Client(ScriptedLLMClient):
    def __init__(self, settings: SessionSettings) -> None:
        super().__init__([])
        self._settings = settings


def _pending_login(url: str = "https://provider.invalid/oauth/authorize?x=1"):
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
            "truecoder.providers.configuration.default_providers_config_path",
        ):
            name = (
                target.rsplit(".", 1)[-1].replace("default_", "").replace("_path", "")
            )
            active = patch(target, return_value=self.root / f"{name}.json")
            active.start()
            self.addCleanup(active.stop)
        directory = patch(
            "truecoder.providers.catalog.load_models_dev",
            return_value=(),
        )
        directory.start()
        self.addCleanup(directory.stop)

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

    async def _pick(self, app, pilot, model=None) -> None:
        app.query_one(PromptInput).text = "/models"
        await pilot.press("enter")
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, ModelPickerScreen),
            description="the model picker",
        )
        app.screen.dismiss(model or CATALOG[0])
        await pilot.pause()

    async def _connect(self, app, pilot, choice: str | None = OAUTH_CHOICE) -> None:
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, CredentialChoiceScreen),
            description="the connection choice",
        )
        app.screen.dismiss(choice)
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

    async def test_the_prompt_never_calls_the_unnamed_provider_default(self):
        screen = ApiKeyScreen("default", "moonshotai/kimi-k2.6")

        explanation = screen._explanation()

        self.assertNotIn("default", explanation)
        self.assertIn("moonshotai/kimi-k2.6", explanation)

    async def test_a_named_provider_is_named_in_the_prompt(self):
        screen = ApiKeyScreen("openrouter", "moonshotai/kimi-k2.6")

        self.assertIn("openrouter", screen._explanation())

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


class CrossProviderTests(_Base):
    def _configure(self, *, brio_oauth: bool) -> None:
        brio: dict[str, object] = {
            "name": "brio",
            "base_url": "https://api.brio.invalid/v1",
        }
        if brio_oauth:
            brio["oauth"] = {
                "client_id": "client-123",
                "authorize_url": "https://provider.invalid/oauth/authorize",
                "token_url": "https://provider.invalid/oauth/token",
            }
        (self.root / "providers_config.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": [
                        {"name": "acme", "base_url": "https://api.acme.invalid/v1"},
                        brio,
                    ],
                }
            ),
            encoding="utf-8",
        )

    @staticmethod
    async def _listing(provider, credential, *, refresh=False):
        return {
            "acme": (ModelInfo(identifier="acme/starter", provider="acme"),),
            "brio": (ModelInfo(identifier="brio/large", provider="brio"),),
        }.get(provider.name, ())

    def _catalog(self):
        return patch(
            "truecoder.providers.catalog.load_models",
            side_effect=self._listing,
        )

    async def test_the_picker_lists_models_from_every_provider(self):
        self._configure(brio_oauth=False)
        app = self._app(_settings(credential=ApiKey("sk-acme")))

        with patch.dict(os.environ, {"MODEL": "acme/starter"}), self._catalog():
            async with app.run_test(size=(120, 40)) as pilot:
                app.query_one(PromptInput).text = "/models"
                await pilot.press("enter")
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ModelPickerScreen),
                    description="the model picker",
                )

                listed = [model.identifier for model in app.screen.models]
                self.assertEqual(listed, ["acme/starter", "brio/large"])
                self.assertTrue(app.screen.spans_providers)
                app.screen.dismiss(None)
                await pilot.pause()

    async def test_a_model_from_another_provider_moves_the_session_to_it(self):
        self._configure(brio_oauth=False)
        app = self._app(_settings(credential=ApiKey("sk-acme")))

        with patch.dict(os.environ, {"MODEL": "acme/starter"}), self._catalog():
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(
                    app,
                    pilot,
                    ModelInfo(identifier="brio/large", provider="brio"),
                )
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ApiKeyScreen),
                    description="the api key prompt",
                )

                settings = app.agent.llm_client.settings
                self.assertEqual(settings.provider.name, "brio")
                self.assertEqual(settings.model, "brio/large")
                self.assertEqual(app.screen.provider, "brio")
                self.assertIn("brio", (self.root / "settings.json").read_text())

                app.screen.dismiss(None)
                await pilot.pause()

    async def test_a_remembered_key_for_that_provider_is_reused_without_asking(self):
        from truecoder.providers.keys import store_key

        self._configure(brio_oauth=False)
        store_key("brio", ApiKey("sk-brio"), self.root / "keys.json")
        app = self._app(_settings(credential=ApiKey("sk-acme")))

        with patch.dict(os.environ, {"MODEL": "acme/starter"}), self._catalog():
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(
                    app,
                    pilot,
                    ModelInfo(identifier="brio/large", provider="brio"),
                )
                await pilot.pause()

                settings = app.agent.llm_client.settings
                self.assertEqual(settings.provider.name, "brio")
                self.assertEqual(settings.credential, ApiKey("sk-brio"))
                self.assertNotIsInstance(app.screen, ApiKeyScreen)

    async def test_a_provider_that_cannot_list_anything_still_offers_a_sign_in(self):
        from truecoder.providers.catalog import CatalogError

        self._configure(brio_oauth=True)
        app = self._app(_settings(credential=ApiKey("sk-acme")))

        async def listing(provider, credential, *, refresh=False):
            if provider.name == "brio":
                raise CatalogError("the provider returned 401")
            return (ModelInfo(identifier="acme/starter", provider="acme"),)

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch(
                "truecoder.providers.catalog.load_models",
                side_effect=listing,
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                app.query_one(PromptInput).text = "/models"
                await pilot.press("enter")
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ModelPickerScreen),
                    description="the model picker",
                )

                invitations = app.screen.invitations
                self.assertEqual([invite.provider for invite in invitations], ["brio"])
                self.assertTrue(invitations[0].oauth)
                self.assertIn("Connect to brio", invitations[0].label)

                app.screen.dismiss(None)
                await pilot.pause()

    async def test_accepting_an_invitation_signs_in_and_reopens_the_picker(self):
        from truecoder.providers.catalog import CatalogError
        from truecoder.tui.model_picker import ProviderInvite

        self._configure(brio_oauth=True)
        app = self._app(_settings(credential=ApiKey("sk-acme")))
        pending = _pending_login()
        signed_in = False

        async def listing(provider, credential, *, refresh=False):
            if provider.name == "brio":
                if not signed_in:
                    raise CatalogError("the provider returned 401")
                return (ModelInfo(identifier="brio/large", provider="brio"),)
            return (ModelInfo(identifier="acme/starter", provider="acme"),)

        async def begin(client, *, provider=""):
            return pending

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", side_effect=listing),
            patch("truecoder.providers.login.begin_login", side_effect=begin),
            patch("truecoder.providers.login.open_in_browser", return_value=True),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot, ProviderInvite("brio", oauth=True))
                await self._connect(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, AuthorisationScreen),
                    description="the authorisation screen",
                )

                self.assertEqual(app.screen.provider, "brio")
                self.assertEqual(app.agent.llm_client.settings.provider.name, "acme")
                self.assertEqual(
                    app.agent.llm_client.settings.credential, ApiKey("sk-acme")
                )

                signed_in = True
                pending.token.set_result(
                    OAuthToken(access_token="at-brio", provider="brio")
                )
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ModelPickerScreen),
                    description="the picker to reopen",
                )

                listed = [model.identifier for model in app.screen.models]
                self.assertIn("brio/large", listed)
                self.assertEqual(app.screen.invitations, ())

                app.screen.dismiss(None)
                await pilot.pause()

    async def test_abandoning_an_invitation_leaves_the_session_where_it_was(self):
        from truecoder.providers.catalog import CatalogError
        from truecoder.tui.model_picker import ProviderInvite

        self._configure(brio_oauth=True)
        app = self._app(_settings(credential=ApiKey("sk-acme")))
        pending = _pending_login()

        async def listing(provider, credential, *, refresh=False):
            if provider.name == "brio":
                raise CatalogError("the provider returned 401")
            return (ModelInfo(identifier="acme/starter", provider="acme"),)

        async def begin(client, *, provider=""):
            return pending

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", side_effect=listing),
            patch("truecoder.providers.login.begin_login", side_effect=begin),
            patch("truecoder.providers.login.open_in_browser", return_value=True),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot, ProviderInvite("brio", oauth=True))
                await self._connect(app, pilot)
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

                settings = app.agent.llm_client.settings
                self.assertEqual(settings.provider.name, "acme")
                self.assertEqual(settings.credential, ApiKey("sk-acme"))
                self.assertEqual(settings.model, "acme/starter")

    async def test_an_oauth_provider_starts_a_sign_in_rather_than_a_key_prompt(self):
        self._configure(brio_oauth=True)
        app = self._app(_settings(credential=ApiKey("sk-acme")))
        pending = _pending_login()
        opened: list[str] = []

        async def begin(client, *, provider=""):
            return pending

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            self._catalog(),
            patch("truecoder.providers.login.begin_login", side_effect=begin),
            patch(
                "truecoder.providers.login.open_in_browser",
                side_effect=lambda target: (opened.append(target), True)[1],
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(
                    app,
                    pilot,
                    ModelInfo(identifier="brio/large", provider="brio"),
                )
                await self._connect(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, AuthorisationScreen),
                    description="the authorisation screen",
                )

                self.assertEqual(app.agent.llm_client.settings.provider.name, "brio")
                self.assertEqual(opened, [pending.url])

                app.screen.dismiss(False)
                await pilot.pause()


class CredentialChoiceTests(_Base):
    async def test_a_provider_with_both_ways_asks_which_one(self):
        app = self._app(_settings(credential=None, oauth=OAUTH))

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, CredentialChoiceScreen),
                    description="the connection choice",
                )

                self.assertEqual(app.screen.provider, "acme")
                self.assertIn("openai/gpt-5", app.screen._explanation())

                app.screen.dismiss(None)
                await pilot.pause()

    async def test_choosing_the_key_never_opens_a_browser(self):
        app = self._app(_settings(credential=None, oauth=OAUTH))
        opened: list[str] = []

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
            patch(
                "truecoder.providers.login.open_in_browser",
                side_effect=lambda target: (opened.append(target), True)[1],
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await self._connect(app, pilot, KEY_CHOICE)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ApiKeyScreen),
                    description="the api key prompt",
                )
                app.screen.dismiss("sk-chosen")
                await wait_until(
                    pilot,
                    lambda: app.agent.llm_client.settings.credential is not None,
                    description="the key to be adopted",
                )

                self.assertEqual(opened, [])
                self.assertEqual(
                    app.agent.llm_client.settings.credential,
                    ApiKey("sk-chosen"),
                )
                self.assertIn("sk-chosen", (self.root / "keys.json").read_text())

    async def test_choosing_the_browser_starts_the_sign_in(self):
        app = self._app(_settings(credential=None, oauth=OAUTH))
        pending = _pending_login()
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
                await self._connect(app, pilot, OAUTH_CHOICE)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, AuthorisationScreen),
                    description="the authorisation screen",
                )

                self.assertEqual(opened, [pending.url])

                app.screen.dismiss(False)
                await pilot.pause()

    async def test_a_provider_with_only_a_key_is_never_asked_to_choose(self):
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

                self.assertNotIsInstance(app.screen, CredentialChoiceScreen)

    async def test_abandoning_the_choice_leaves_the_credential_alone(self):
        app = self._app(_settings(credential=None, oauth=OAUTH))
        notices: list[str] = []

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
            patch.object(
                TrueCoderApp,
                "notify",
                lambda self, message, **kwargs: notices.append(str(message)),
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await self._connect(app, pilot, None)
                await pilot.pause()

                self.assertIsNone(app.agent.llm_client.settings.credential)
                self.assertNotIsInstance(app.screen, ApiKeyScreen)
                self.assertTrue(any("not connected" in note for note in notices))

    async def test_login_offers_the_same_choice(self):
        app = self._app(_settings(credential=None, oauth=OAUTH))

        with patch.dict(os.environ, {"MODEL": "acme/starter"}):
            async with app.run_test(size=(120, 40)) as pilot:
                app.query_one(PromptInput).text = "/login"
                await pilot.press("enter")
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, CredentialChoiceScreen),
                    description="the connection choice",
                )

                app.screen.dismiss(None)
                await pilot.pause()

    async def test_the_choice_never_calls_the_unnamed_provider_default(self):
        screen = CredentialChoiceScreen("default")

        self.assertNotIn("default", screen._explanation())

    async def test_both_ways_are_offered_by_name(self):
        app = self._app(_settings(credential=None, oauth=OAUTH))
        screen = CredentialChoiceScreen("acme")

        with patch.dict(os.environ, {"MODEL": "acme/starter"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await app.push_screen(screen)
                await pilot.pause()
                await wait_until(
                    pilot,
                    lambda: screen.query_one("#choose-key").region.width > 0,
                    description="the buttons to be laid out",
                )

                rendered = "".join(
                    "".join(segment.text for segment in strip)
                    for strip in app.screen._compositor.render_strips()
                )

                self.assertIn("Browser sign-in", rendered)
                self.assertIn("API key", rendered)
                self.assertIn("esc cancel", rendered)


class DeviceCodeTests(_Base):
    def _oauth(self, *, device: bool):
        return OAuthClient(
            client_id="client-123",
            authorize_url="https://provider.invalid/oauth/authorize",
            token_url="https://provider.invalid/oauth/token",
            device_url="https://provider.invalid/oauth/device" if device else "",
        )

    def _grant(self):
        from truecoder.providers.device import DeviceGrant

        return DeviceGrant(
            device_code="dc-1",
            user_code="WDJB-MJHT",
            verification_url="https://provider.invalid/activate",
        )

    async def test_a_provider_without_a_device_url_never_offers_the_code(self):
        app = self._app(_settings(credential=None, oauth=self._oauth(device=False)))

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, CredentialChoiceScreen),
                    description="the connection choice",
                )

                self.assertFalse(app.screen.device)
                self.assertFalse(app.screen.query("#choose-device"))

                app.screen.dismiss(None)
                await pilot.pause()

    async def test_a_device_provider_offers_a_third_way(self):
        app = self._app(_settings(credential=None, oauth=self._oauth(device=True)))

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, CredentialChoiceScreen),
                    description="the connection choice",
                )

                self.assertTrue(app.screen.device)
                self.assertTrue(app.screen.query("#choose-device"))

                app.screen.dismiss(None)
                await pilot.pause()

    async def test_a_code_is_shown_and_the_token_is_adopted(self):
        app = self._app(_settings(credential=None, oauth=self._oauth(device=True)))
        grant = self._grant()
        approved = asyncio.get_event_loop().create_future()

        async def request(client):
            return grant

        async def poll(client, issued, *, provider="", **rest):
            return await approved

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
            patch(
                "truecoder.providers.device.request_device_grant", side_effect=request
            ),
            patch("truecoder.providers.device.poll_device_grant", side_effect=poll),
            patch("truecoder.providers.login.open_in_browser", return_value=True),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await self._connect(app, pilot, DEVICE_CHOICE)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, DeviceCodeScreen),
                    description="the device code screen",
                )

                self.assertEqual(app.screen.user_code, "WDJB-MJHT")
                notes = list(app.query(SystemNote))
                self.assertTrue(any("WDJB-MJHT" in note.message for note in notes))

                approved.set_result(
                    OAuthToken(access_token="at-device", provider="acme")
                )
                await wait_until(
                    pilot,
                    lambda: app.agent.llm_client.settings.credential is not None,
                    description="the token to be adopted",
                )

                self.assertIn(
                    "at-device",
                    (self.root / "tokens.json").read_text(),
                )

    async def test_the_code_can_be_copied(self):
        app = self._app(_settings(credential=None, oauth=self._oauth(device=True)))
        screen = DeviceCodeScreen("acme", "WDJB-MJHT", "https://provider.invalid/a")

        with patch.dict(os.environ, {"MODEL": "acme/starter"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await app.push_screen(screen)
                await pilot.pause()
                await pilot.press("c")
                await pilot.pause()

                self.assertEqual(app.clipboard, "WDJB-MJHT")

    async def test_cancelling_the_code_leaves_the_credential_alone(self):
        app = self._app(_settings(credential=None, oauth=self._oauth(device=True)))
        grant = self._grant()
        waiting = asyncio.get_event_loop().create_future()

        async def request(client):
            return grant

        async def poll(client, issued, *, provider="", **rest):
            return await waiting

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch("truecoder.providers.catalog.load_models", return_value=CATALOG),
            patch(
                "truecoder.providers.device.request_device_grant", side_effect=request
            ),
            patch("truecoder.providers.device.poll_device_grant", side_effect=poll),
            patch("truecoder.providers.login.open_in_browser", return_value=True),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._pick(app, pilot)
                await self._connect(app, pilot, DEVICE_CHOICE)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, DeviceCodeScreen),
                    description="the device code screen",
                )

                app.screen.dismiss(False)
                await wait_until(
                    pilot,
                    lambda: not isinstance(app.screen, DeviceCodeScreen),
                    description="the device screen to close",
                )

                self.assertIsNone(app.agent.llm_client.settings.credential)

    async def test_a_refused_grant_is_reported_not_raised(self):
        app = self._app(_settings(credential=None, oauth=self._oauth(device=True)))
        notices: list[str] = []

        async def request(client):
            raise OAuthError("the provider refused a device code")

        with (
            patch.dict(os.environ, {"MODEL": "acme/starter"}),
            patch(
                "truecoder.providers.device.request_device_grant", side_effect=request
            ),
            patch.object(
                TrueCoderApp,
                "notify",
                lambda self, message, **kwargs: notices.append(str(message)),
            ),
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await app._authorise_by_code(app.agent.llm_client.settings.provider)
                await pilot.pause()

                self.assertTrue(any("refused a device code" in n for n in notices))


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
        return _pending_login(url)

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
                await self._connect(app, pilot)
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
                await self._connect(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, AuthorisationScreen),
                    description="the authorisation screen",
                )
                await wait_until(
                    pilot,
                    lambda: bool(app.screen._compositor.render_strips()),
                    description="the authorisation screen to render",
                )

                rendered = "".join(
                    "".join(segment.text for segment in strip)
                    for strip in app.screen._compositor.render_strips()
                )

                self.assertFalse(app.screen.browser_opened)
                self.assertIn(BROWSER_REFUSED[:24], rendered)
                self.assertIn(pending.url, rendered)

                app.screen.dismiss(False)
                await pilot.pause()

    async def test_the_link_outlives_the_dialog_in_the_transcript(self):
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
                await self._connect(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, AuthorisationScreen),
                    description="the authorisation screen",
                )

                app.screen.dismiss(False)
                await wait_until(
                    pilot,
                    lambda: not isinstance(app.screen, AuthorisationScreen),
                    description="the authorisation screen to close",
                )

                notes = list(app.query(SystemNote))
                self.assertEqual(len(notes), 1)
                self.assertEqual(notes[0].detail, pending.url)

                rendered = "".join(
                    "".join(segment.text for segment in strip)
                    for strip in app.screen._compositor.render_strips()
                )
                self.assertIn("provider.invalid", rendered)

    async def test_a_new_chat_clears_the_sign_in_note(self):
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
                await self._connect(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, AuthorisationScreen),
                    description="the authorisation screen",
                )
                app.screen.dismiss(False)
                await wait_until(
                    pilot,
                    lambda: bool(app.query(SystemNote)),
                    description="the sign-in note",
                )

                await app.action_new_chat()
                await pilot.pause()

                self.assertEqual(list(app.query(SystemNote)), [])

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
                await self._connect(app, pilot)
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
                await self._connect(app, pilot)
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
