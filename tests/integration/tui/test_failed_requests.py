"""A refusal from the provider must be readable and point at the way out."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers.tui import wait_until
from tests.integration.tui.test_app import FixedTokenCounter, ScriptedLLMClient
from truecoder.agent import Agent, ContextBuilder
from truecoder.client.failures import classify
from truecoder.client.response import EventType, StreamEvent
from truecoder.providers import ApiKey, Provider, SessionSettings
from truecoder.providers.oauth import OAuthClient
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.credentials import ApiKeyScreen
from truecoder.tui.widgets import ChatMessage, PromptInput

CREDITS = (
    "This request requires more credits, or fewer max_tokens. You requested up "
    "to 65536 tokens, but can only afford 3325."
)

OAUTH = OAuthClient(
    client_id="client-123",
    authorize_url="https://provider.invalid/oauth/authorize",
    token_url="https://provider.invalid/oauth/token",
)


class _Client(ScriptedLLMClient):
    def __init__(self, settings: SessionSettings, failure) -> None:
        super().__init__([])
        self._settings = settings
        self._failure = failure

    async def chat_completion(self, messages, stream=True, tools=None):
        self.calls.append((messages, stream))
        yield StreamEvent(
            type=EventType.ERROR,
            error=self._failure.message,
            failure=self._failure,
        )


def _settings(*, oauth=None) -> SessionSettings:
    return SessionSettings(
        provider=Provider(
            name="acme",
            base_url="https://api.acme.invalid/v1",
            oauth=oauth,
        ),
        credential=ApiKey("sk-rejected"),
        model="acme/starter",
    )


class FailedRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

        for target, name in (
            ("truecoder.providers.store.default_settings_path", "settings.json"),
            ("truecoder.providers.keys.default_keys_path", "keys.json"),
            ("truecoder.providers.tokens.default_tokens_path", "tokens.json"),
        ):
            active = patch(target, return_value=self.root / name)
            active.start()
            self.addCleanup(active.stop)

    def _app(self, failure, *, oauth=None) -> TrueCoderApp:
        agent = Agent(
            llm_client=_Client(_settings(oauth=oauth), failure),
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
        )
        return TrueCoderApp(agent)

    async def _send(self, app, pilot) -> ChatMessage:
        app.query_one(PromptInput).text = "hello"
        await pilot.press("enter")
        await wait_until(
            pilot,
            lambda: any(
                "Request failed" in message.content_text
                for message in app.query(ChatMessage)
            ),
            description="the failure to be rendered",
        )
        return next(
            message
            for message in app.query(ChatMessage)
            if "Request failed" in message.content_text
        )

    async def test_a_billing_refusal_reads_as_a_sentence(self):
        failure = classify(
            status=402,
            body={"error": {"message": CREDITS, "code": 402, "metadata": {}}},
            provider="acme",
        )
        app = self._app(failure)

        with patch.dict(os.environ, {"MODEL": "acme/starter"}):
            async with app.run_test(size=(120, 40)) as pilot:
                shown = (await self._send(app, pilot)).content_text

                self.assertIn("acme refused the request over billing", shown)
                self.assertIn("65536 tokens", shown)
                self.assertNotIn("'metadata'", shown)
                self.assertNotIn("{", shown)

    async def test_a_billing_refusal_suggests_credit_not_a_new_key(self):
        failure = classify(status=402, provider="acme")
        app = self._app(failure)

        with patch.dict(os.environ, {"MODEL": "acme/starter"}):
            async with app.run_test(size=(120, 40)) as pilot:
                shown = (await self._send(app, pilot)).content_text
                await pilot.pause()

                self.assertIn("Add credit", shown)
                self.assertNotIsInstance(app.screen, ApiKeyScreen)

    async def test_a_rejected_key_asks_for_another_one(self):
        failure = classify(status=401, provider="acme")
        app = self._app(failure)

        with patch.dict(os.environ, {"MODEL": "acme/starter"}):
            async with app.run_test(size=(120, 40)) as pilot:
                shown = (await self._send(app, pilot)).content_text
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ApiKeyScreen),
                    description="the api key prompt",
                )

                self.assertIn("rejected the credential", shown)
                self.assertIn("rejected the key in use", app.screen._explanation())
                self.assertEqual(app.screen.provider, "acme")

                app.screen.dismiss(None)
                await pilot.pause()

    async def test_a_replacement_key_is_adopted_and_saved(self):
        failure = classify(status=401, provider="acme")
        app = self._app(failure)

        with patch.dict(os.environ, {"MODEL": "acme/starter"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._send(app, pilot)
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ApiKeyScreen),
                    description="the api key prompt",
                )
                app.screen.dismiss("sk-replacement")
                await wait_until(
                    pilot,
                    lambda: app.agent.llm_client.settings.credential
                    == ApiKey("sk-replacement"),
                    description="the replacement key to be adopted",
                )

                self.assertIn(
                    "sk-replacement",
                    (self.root / "keys.json").read_text(),
                )

    async def test_an_oauth_provider_is_told_to_run_login_instead(self):
        failure = classify(status=401, provider="acme")
        app = self._app(failure, oauth=OAUTH)

        with patch.dict(os.environ, {"MODEL": "acme/starter"}):
            async with app.run_test(size=(120, 40)) as pilot:
                shown = (await self._send(app, pilot)).content_text
                await pilot.pause()

                self.assertIn("/login", shown)
                self.assertNotIsInstance(app.screen, ApiKeyScreen)

    async def test_an_unclassified_failure_invents_no_advice(self):
        failure = classify(status=None, fallback="something went wrong", provider="acme")
        app = self._app(failure)

        with patch.dict(os.environ, {"MODEL": "acme/starter"}):
            async with app.run_test(size=(120, 40)) as pilot:
                shown = (await self._send(app, pilot)).content_text
                await pilot.pause()

                self.assertIn("something went wrong", shown)
                self.assertNotIn("/login", shown)
                self.assertNotIsInstance(app.screen, ApiKeyScreen)

    async def test_the_composer_is_usable_again_after_a_failure(self):
        failure = classify(status=402, provider="acme")
        app = self._app(failure)

        with patch.dict(os.environ, {"MODEL": "acme/starter"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await self._send(app, pilot)
                await pilot.pause()

                self.assertFalse(app._busy)
                self.assertTrue(app.query_one(PromptInput).has_focus)


if __name__ == "__main__":
    unittest.main()
