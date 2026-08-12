"""A token that expires mid-session must renew itself rather than start failing."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from truecoder.client.llm_client import LLMClient
from truecoder.providers import ApiKey, Provider, SessionSettings
from truecoder.providers.oauth import OAuthClient, OAuthError, OAuthToken

CLIENT = OAuthClient(
    client_id="client-123",
    authorize_url="https://provider.invalid/oauth/authorize",
    token_url="https://provider.invalid/oauth/token",
)


def _settings(credential, *, oauth: OAuthClient | None = CLIENT) -> SessionSettings:
    return SessionSettings(
        provider=Provider(
            name="acme",
            base_url="https://api.acme.invalid/v1",
            oauth=oauth,
        ),
        credential=credential,
        model="acme/starter",
    )


def _now() -> float:
    return time.time()


def _token(*, expires_in: float, refresh: str | None = "rt-1") -> OAuthToken:
    return OAuthToken(
        access_token="at-old",
        refresh_token=refresh,
        expires_at=_now() + expires_in,
        provider="acme",
    )


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)
        active = patch(
            "truecoder.providers.tokens.default_tokens_path",
            return_value=self.root / "tokens.json",
        )
        active.start()
        self.addCleanup(active.stop)

    async def test_a_token_near_expiry_is_renewed_before_the_request(self):
        settings = _settings(_token(expires_in=5))
        client = LLMClient(settings)
        fresh = OAuthToken(
            access_token="at-new",
            refresh_token="rt-2",
            expires_at=_now() + 3600,
            provider="acme",
        )

        async def renew(oauth, token):
            return fresh

        with patch("truecoder.client.llm_client.refresh_token", side_effect=renew):
            renewed = await client.refresh_credential()

        self.assertTrue(renewed)
        self.assertEqual(settings.credential, fresh)
        self.assertIn("at-new", (self.root / "tokens.json").read_text())

    async def test_a_healthy_token_is_left_alone(self):
        settings = _settings(_token(expires_in=3600))
        client = LLMClient(settings)
        calls: list[object] = []

        async def renew(oauth, token):
            calls.append(token)
            raise AssertionError("a healthy token must not be refreshed")

        with patch("truecoder.client.llm_client.refresh_token", side_effect=renew):
            renewed = await client.refresh_credential()

        self.assertFalse(renewed)
        self.assertEqual(calls, [])

    async def test_an_api_key_is_never_refreshed(self):
        settings = _settings(ApiKey("sk-1"))
        client = LLMClient(settings)

        async def renew(oauth, token):
            raise AssertionError("a key has nothing to refresh")

        with patch("truecoder.client.llm_client.refresh_token", side_effect=renew):
            self.assertFalse(await client.refresh_credential())

    async def test_a_token_without_a_refresh_token_is_left_alone(self):
        settings = _settings(_token(expires_in=5, refresh=None))
        client = LLMClient(settings)

        async def renew(oauth, token):
            raise AssertionError("nothing to refresh with")

        with patch("truecoder.client.llm_client.refresh_token", side_effect=renew):
            self.assertFalse(await client.refresh_credential())

    async def test_a_provider_without_an_oauth_client_cannot_refresh(self):
        settings = _settings(_token(expires_in=5), oauth=None)
        client = LLMClient(settings)

        self.assertFalse(await client.refresh_credential())

    async def test_a_failed_refresh_keeps_the_old_token(self):
        stale = _token(expires_in=5)
        settings = _settings(stale)
        client = LLMClient(settings)

        async def renew(oauth, token):
            raise OAuthError("the provider refused")

        with patch("truecoder.client.llm_client.refresh_token", side_effect=renew):
            self.assertFalse(await client.refresh_credential())

        self.assertEqual(settings.credential, stale)
        self.assertFalse((self.root / "tokens.json").exists())

    async def test_concurrent_turns_refresh_once(self):
        settings = _settings(_token(expires_in=5))
        client = LLMClient(settings)
        attempts = 0

        async def renew(oauth, token):
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(0.01)
            return OAuthToken(
                access_token=f"at-{attempts}",
                refresh_token="rt-2",
                expires_at=_now() + 3600,
                provider="acme",
            )

        with patch("truecoder.client.llm_client.refresh_token", side_effect=renew):
            await asyncio.gather(*(client.refresh_credential() for _ in range(8)))

        self.assertEqual(attempts, 1)

    async def test_refreshing_drops_the_cached_connection(self):
        settings = _settings(_token(expires_in=5))
        client = LLMClient(settings)
        invalidated: list[bool] = []
        settings.on_connection_change(lambda: invalidated.append(True))

        async def renew(oauth, token):
            return OAuthToken(
                access_token="at-new",
                refresh_token="rt-2",
                expires_at=_now() + 3600,
                provider="acme",
            )

        with patch("truecoder.client.llm_client.refresh_token", side_effect=renew):
            await client.refresh_credential()

        self.assertTrue(invalidated)


if __name__ == "__main__":
    unittest.main()
