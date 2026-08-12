"""A registered client only accepts the redirect URI it was registered with."""

from __future__ import annotations

import asyncio
import unittest

from truecoder.providers.login import CallbackServer, begin_login
from truecoder.providers.oauth import OAuthClient, OAuthError


def _client(port: int) -> OAuthClient:
    return OAuthClient(
        client_id="client-123",
        authorize_url="https://provider.invalid/oauth/authorize",
        token_url="https://provider.invalid/oauth/token",
        redirect_port=port,
    )


async def _free_port() -> int:
    server = await asyncio.start_server(lambda r, w: None, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


class RedirectPortTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unset_port_still_picks_a_free_one(self):
        pending = await begin_login(_client(0), provider="acme")
        try:
            self.assertGreater(pending.server.port, 0)
            self.assertIn(f"127.0.0.1%3A{pending.server.port}%2Fcallback", pending.url)
        finally:
            await pending.close()

    async def test_a_declared_port_is_the_one_bound(self):
        port = await _free_port()

        pending = await begin_login(_client(port), provider="acme")
        try:
            self.assertEqual(pending.server.port, port)
            self.assertIn(f"127.0.0.1%3A{port}%2Fcallback", pending.url)
        finally:
            await pending.close()

    async def test_a_taken_port_is_reported_by_number(self):
        port = await _free_port()
        occupied = CallbackServer(expected_state="state-1", port=port)
        await occupied.start()

        try:
            with self.assertRaises(OAuthError) as caught:
                await begin_login(_client(port), provider="acme")

            self.assertIn(str(port), str(caught.exception))
            self.assertIn("already in use", str(caught.exception))
        finally:
            await occupied.stop()

    async def test_a_port_outside_the_range_is_refused(self):
        with self.assertRaises(OAuthError):
            _client(70000)

    async def test_a_negative_port_is_refused(self):
        with self.assertRaises(OAuthError):
            _client(-1)


if __name__ == "__main__":
    unittest.main()
