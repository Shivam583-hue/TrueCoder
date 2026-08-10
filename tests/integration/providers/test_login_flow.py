"""The browser round trip runs against the real callback server."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx

from truecoder.providers.login import authorise, begin_login
from truecoder.providers.oauth import OAuthClient, OAuthError

CLIENT = OAuthClient(
    client_id="client-123",
    authorize_url="https://provider.invalid/oauth/authorize",
    token_url="https://provider.invalid/oauth/token",
)


class _Browser:
    def __init__(self, *, tamper_state: bool = False, error: str | None = None):
        self.tamper_state = tamper_state
        self.error = error
        self.seen: list[str] = []
        self.thread: threading.Thread | None = None

    def __call__(self, target: str) -> None:
        self.seen.append(target)
        query = parse_qs(urlparse(target).query)
        redirect = query["redirect_uri"][0]
        state = "wrong" if self.tamper_state else query["state"][0]

        if self.error is not None:
            callback = f"{redirect}?error={self.error}&state={state}"
        else:
            callback = f"{redirect}?code=auth-code-1&state={state}"

        def visit() -> None:
            try:
                httpx.get(callback, timeout=5.0)
            except httpx.HTTPError:
                return

        self.thread = threading.Thread(target=visit, daemon=True)
        self.thread.start()


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    async def _authorise(self, browser: _Browser, payload: dict | None = None):
        async def fake_post(client, body):
            self.exchanged = body
            return payload or {
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "expires_in": 3600,
            }

        with patch("truecoder.providers.login.post_token", side_effect=fake_post):
            return await authorise(
                CLIENT,
                provider="test",
                open_browser=browser,
                timeout=10.0,
            )

    async def test_a_complete_round_trip_yields_a_token(self):
        browser = _Browser()

        token = await self._authorise(browser)

        self.assertEqual(token.access_token, "at-1")
        self.assertEqual(token.refresh_token, "rt-1")
        self.assertEqual(token.provider, "test")

    async def test_the_verifier_is_exchanged_but_never_sent_to_the_browser(self):
        browser = _Browser()

        await self._authorise(browser)

        verifier = self.exchanged["code_verifier"]
        self.assertTrue(verifier)
        self.assertNotIn(verifier, browser.seen[0])
        self.assertEqual(self.exchanged["code"], "auth-code-1")
        self.assertEqual(self.exchanged["grant_type"], "authorization_code")

    async def test_the_redirect_uri_matches_the_one_that_was_authorised(self):
        browser = _Browser()

        await self._authorise(browser)

        announced = parse_qs(urlparse(browser.seen[0]).query)["redirect_uri"][0]
        self.assertEqual(self.exchanged["redirect_uri"], announced)

    async def test_a_tampered_state_is_refused(self):
        with self.assertRaises(OAuthError) as caught:
            await self._authorise(_Browser(tamper_state=True))

        self.assertIn("state", str(caught.exception))

    async def test_a_provider_refusal_surfaces(self):
        with self.assertRaises(OAuthError) as caught:
            await self._authorise(_Browser(error="access_denied"))

        self.assertIn("access_denied", str(caught.exception))

    async def test_the_callback_port_is_released_afterwards(self):
        browser = _Browser()

        await self._authorise(browser)

        port = urlparse(
            parse_qs(urlparse(browser.seen[0]).query)["redirect_uri"][0]
        ).port
        assert port is not None
        async with httpx.AsyncClient(timeout=2.0) as client:
            with self.assertRaises(httpx.TransportError):
                await client.get(f"http://127.0.0.1:{port}/callback")


class PendingLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_url_is_known_before_anyone_waits(self):
        pending = await begin_login(CLIENT, provider="test")
        self.addAsyncCleanup(pending.close)

        query = parse_qs(urlparse(pending.url).query)

        self.assertTrue(pending.url.startswith(CLIENT.authorize_url))
        self.assertEqual(query["client_id"], ["client-123"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertIn("state", query)

    async def test_the_verifier_never_appears_in_the_url_that_is_shown(self):
        pending = await begin_login(CLIENT, provider="test")
        self.addAsyncCleanup(pending.close)

        self.assertNotIn(pending.verifier, pending.url)

    async def test_the_callback_is_listening_as_soon_as_the_url_exists(self):
        pending = await begin_login(CLIENT, provider="test")
        self.addAsyncCleanup(pending.close)

        redirect = parse_qs(urlparse(pending.url).query)["redirect_uri"][0]
        state = parse_qs(urlparse(pending.url).query)["state"][0]

        async def fake_post(client, body):
            self.exchanged = body
            return {"access_token": "at-1"}

        def visit() -> None:
            try:
                httpx.get(f"{redirect}?code=auth-code-1&state={state}", timeout=5.0)
            except httpx.HTTPError:
                return

        threading.Thread(target=visit, daemon=True).start()
        with patch("truecoder.providers.login.post_token", side_effect=fake_post):
            token = await pending.wait(timeout=10.0)

        self.assertEqual(token.access_token, "at-1")
        self.assertEqual(self.exchanged["code_verifier"], pending.verifier)

    async def test_closing_without_waiting_releases_the_port(self):
        pending = await begin_login(CLIENT, provider="test")
        port = pending.server.port
        await pending.close()

        async with httpx.AsyncClient(timeout=2.0) as client:
            with self.assertRaises(httpx.TransportError):
                await client.get(f"http://127.0.0.1:{port}/callback")

    async def test_a_client_that_is_not_a_client_is_refused(self):
        with self.assertRaises(OAuthError):
            await begin_login("not a client", provider="test")


if __name__ == "__main__":
    unittest.main()
