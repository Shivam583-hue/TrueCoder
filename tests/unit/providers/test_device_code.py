"""A machine with no browser must still be able to finish a sign-in."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from truecoder.providers.device import (
    DEFAULT_INTERVAL_SECONDS,
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    DeviceGrant,
    device_body,
    parse_device_grant,
    poll_device_grant,
)
from truecoder.providers.oauth import OAuthClient, OAuthError

CLIENT = OAuthClient(
    client_id="client-123",
    authorize_url="https://provider.invalid/oauth/authorize",
    token_url="https://provider.invalid/oauth/token",
    device_url="https://provider.invalid/oauth/device",
)

GRANT = {
    "device_code": "dc-1",
    "user_code": "WDJB-MJHT",
    "verification_uri": "https://provider.invalid/activate",
    "interval": 5,
    "expires_in": 600,
}


class GrantParsingTests(unittest.TestCase):
    def test_a_well_formed_grant_is_kept(self):
        grant = parse_device_grant(GRANT)

        self.assertEqual(grant.user_code, "WDJB-MJHT")
        self.assertEqual(grant.verification_url, "https://provider.invalid/activate")
        self.assertEqual(grant.interval, 5)

    def test_the_complete_url_is_preferred_when_offered(self):
        grant = parse_device_grant(
            {**GRANT, "verification_uri_complete": "https://provider.invalid/a?c=WDJB"}
        )

        self.assertEqual(grant.best_url, "https://provider.invalid/a?c=WDJB")

    def test_the_plain_url_is_used_when_there_is_no_complete_one(self):
        self.assertEqual(
            parse_device_grant(GRANT).best_url, "https://provider.invalid/activate"
        )

    def test_a_missing_field_is_refused(self):
        for field in ("device_code", "user_code", "verification_uri"):
            payload = {key: value for key, value in GRANT.items() if key != field}
            with self.assertRaises(OAuthError):
                parse_device_grant(payload)

    def test_an_error_payload_is_refused(self):
        with self.assertRaises(OAuthError):
            parse_device_grant({"error": "invalid_client"})

    def test_a_non_object_payload_is_refused(self):
        with self.assertRaises(OAuthError):
            parse_device_grant(["not", "an", "object"])

    def test_an_absent_interval_falls_back(self):
        payload = {key: value for key, value in GRANT.items() if key != "interval"}

        self.assertEqual(parse_device_grant(payload).interval, DEFAULT_INTERVAL_SECONDS)

    def test_an_absurd_interval_is_clamped(self):
        self.assertEqual(
            parse_device_grant({**GRANT, "interval": 9999}).interval,
            MAX_INTERVAL_SECONDS,
        )
        self.assertEqual(
            parse_device_grant({**GRANT, "interval": 0}).interval,
            DEFAULT_INTERVAL_SECONDS,
        )
        self.assertEqual(
            parse_device_grant({**GRANT, "interval": -3}).interval,
            DEFAULT_INTERVAL_SECONDS,
        )

    def test_an_interval_sent_as_text_is_read(self):
        self.assertEqual(parse_device_grant({**GRANT, "interval": "1"}).interval, 1)
        self.assertGreaterEqual(
            parse_device_grant({**GRANT, "interval": "0.1"}).interval,
            MIN_INTERVAL_SECONDS,
        )

    def test_the_body_names_the_grant_type(self):
        body = device_body(CLIENT, device_code="dc-1")

        self.assertEqual(
            body["grant_type"], "urn:ietf:params:oauth:grant-type:device_code"
        )
        self.assertEqual(body["device_code"], "dc-1")


class PollingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.grant = parse_device_grant(GRANT)
        self.slept: list[float] = []

    async def _sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    async def test_an_approved_code_returns_the_token(self):
        replies = [
            {"error": "authorization_pending"},
            {"error": "authorization_pending"},
            {"access_token": "at-1", "refresh_token": "rt-1"},
        ]

        async def post(client, body):
            return replies.pop(0)

        with patch("truecoder.providers.device.post_token", side_effect=post):
            token = await poll_device_grant(
                CLIENT, self.grant, provider="acme", sleep=self._sleep
            )

        self.assertEqual(token.access_token, "at-1")
        self.assertEqual(self.slept, [5, 5])

    async def test_slow_down_backs_off(self):
        replies = [
            {"error": "slow_down"},
            {"error": "slow_down"},
            {"access_token": "at-1"},
        ]

        async def post(client, body):
            return replies.pop(0)

        with patch("truecoder.providers.device.post_token", side_effect=post):
            await poll_device_grant(CLIENT, self.grant, sleep=self._sleep)

        self.assertEqual(self.slept, [10, 15])

    async def test_a_declined_request_says_so(self):
        async def post(client, body):
            return {"error": "access_denied"}

        with (
            patch("truecoder.providers.device.post_token", side_effect=post),
            self.assertRaises(OAuthError) as caught,
        ):
                await poll_device_grant(CLIENT, self.grant, sleep=self._sleep)

        self.assertIn("declined", str(caught.exception))

    async def test_an_expired_code_says_so(self):
        async def post(client, body):
            return {"error": "expired_token"}

        with (
            patch("truecoder.providers.device.post_token", side_effect=post),
            self.assertRaises(OAuthError) as caught,
        ):
                await poll_device_grant(CLIENT, self.grant, sleep=self._sleep)

        self.assertIn("expired", str(caught.exception))

    async def test_an_unknown_error_stops_rather_than_spinning(self):
        async def post(client, body):
            return {"error": "invalid_client"}

        with (
            patch("truecoder.providers.device.post_token", side_effect=post),
            self.assertRaises(OAuthError),
        ):
                await poll_device_grant(CLIENT, self.grant, sleep=self._sleep)

    async def test_polling_stops_at_the_deadline(self):
        moment = [0.0]

        def clock() -> float:
            return moment[0]

        async def sleeping(seconds: float) -> None:
            moment[0] += seconds

        async def post(client, body):
            return {"error": "authorization_pending"}

        grant = DeviceGrant(
            device_code="dc-1",
            user_code="WDJB",
            verification_url="https://provider.invalid/activate",
            interval=5,
            expires_in=20,
        )

        with (
            patch("truecoder.providers.device.post_token", side_effect=post),
            self.assertRaises(OAuthError) as caught,
        ):
                await poll_device_grant(CLIENT, grant, sleep=sleeping, clock=clock)

        self.assertIn("expired", str(caught.exception))

    async def test_a_provider_without_a_device_url_is_refused(self):
        from truecoder.providers.device import request_device_grant

        plain = OAuthClient(
            client_id="client-123",
            authorize_url="https://provider.invalid/oauth/authorize",
            token_url="https://provider.invalid/oauth/token",
        )

        with self.assertRaises(OAuthError):
            await request_device_grant(plain)


if __name__ == "__main__":
    unittest.main()
