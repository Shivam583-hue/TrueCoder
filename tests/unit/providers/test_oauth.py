"""PKCE, the callback, and tokens that expire and refresh."""

from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from truecoder.providers.login import CallbackServer, authorise, request_target
from truecoder.providers.oauth import (
    REFRESH_MARGIN_SECONDS,
    OAuthClient,
    OAuthError,
    OAuthToken,
    authorization_url,
    exchange_body,
    generate_pkce,
    parse_token_response,
    read_callback,
    refresh_body,
)
from truecoder.providers.tokens import (
    encode_tokens,
    forget_token,
    load_tokens,
    parse_tokens,
    save_tokens,
    store_token,
)

CLIENT = OAuthClient(
    client_id="client-123",
    authorize_url="https://provider.invalid/oauth/authorize",
    token_url="https://provider.invalid/oauth/token",
    scopes=("read", "write"),
)


class OAuthClientTests(unittest.TestCase):
    def test_every_endpoint_is_required(self):
        for missing in ("client_id", "authorize_url", "token_url"):
            values = {
                "client_id": "a",
                "authorize_url": "https://x.invalid/a",
                "token_url": "https://x.invalid/t",
            }
            values[missing] = "  "
            with self.subTest(missing=missing), self.assertRaises(OAuthError):
                OAuthClient(**values)

    def test_endpoints_must_be_https(self):
        with self.assertRaises(OAuthError):
            OAuthClient(
                client_id="a",
                authorize_url="http://x.invalid/a",
                token_url="https://x.invalid/t",
            )


class PkceTests(unittest.TestCase):
    def test_the_challenge_is_the_sha256_of_the_verifier(self):
        pair = generate_pkce()

        expected = (
            base64.urlsafe_b64encode(
                hashlib.sha256(pair.verifier.encode("ascii")).digest()
            )
            .decode("ascii")
            .rstrip("=")
        )
        self.assertEqual(pair.challenge, expected)
        self.assertEqual(pair.method, "S256")

    def test_each_verifier_is_fresh(self):
        self.assertNotEqual(generate_pkce().verifier, generate_pkce().verifier)

    def test_the_verifier_has_no_padding(self):
        self.assertNotIn("=", generate_pkce().verifier)


class AuthorizationUrlTests(unittest.TestCase):
    def test_every_required_parameter_is_present(self):
        pkce = generate_pkce()

        target = authorization_url(
            CLIENT,
            redirect_uri="http://127.0.0.1:9/callback",
            pkce=pkce,
            state="state-1",
        )

        query = parse_qs(urlparse(target).query)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["client-123"])
        self.assertEqual(query["code_challenge"], [pkce.challenge])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["state"], ["state-1"])
        self.assertEqual(query["scope"], ["read write"])

    def test_the_verifier_never_leaves_the_process(self):
        pkce = generate_pkce()

        target = authorization_url(
            CLIENT,
            redirect_uri="http://127.0.0.1:9/callback",
            pkce=pkce,
            state="s",
        )

        self.assertNotIn(pkce.verifier, target)

    def test_an_existing_query_string_is_preserved(self):
        client = OAuthClient(
            client_id="a",
            authorize_url="https://x.invalid/a?tenant=acme",
            token_url="https://x.invalid/t",
        )

        target = authorization_url(
            client,
            redirect_uri="http://127.0.0.1:9/callback",
            pkce=generate_pkce(),
            state="s",
        )

        self.assertEqual(parse_qs(urlparse(target).query)["tenant"], ["acme"])


class CallbackTests(unittest.TestCase):
    def test_a_matching_state_yields_the_code(self):
        result = read_callback("/callback?code=abc&state=s1", "s1")

        self.assertEqual(result.code, "abc")
        self.assertIsNone(result.error)

    def test_a_mismatched_state_is_refused(self):
        result = read_callback("/callback?code=abc&state=other", "s1")

        self.assertIsNone(result.code)
        self.assertIn("state", str(result.error))

    def test_a_missing_state_is_refused(self):
        self.assertIsNotNone(read_callback("/callback?code=abc", "s1").error)

    def test_a_provider_error_is_carried(self):
        result = read_callback(
            "/callback?error=access_denied&error_description=nope&state=s1",
            "s1",
        )

        self.assertIn("access_denied", str(result.error))
        self.assertIn("nope", str(result.error))

    def test_a_missing_code_is_refused(self):
        self.assertIsNotNone(read_callback("/callback?state=s1", "s1").error)

    def test_a_request_line_yields_its_target(self):
        self.assertEqual(
            request_target("GET /callback?code=a HTTP/1.1"),
            "/callback?code=a",
        )

    def test_a_malformed_request_line_yields_nothing(self):
        self.assertEqual(request_target("garbage"), "")


class TokenResponseTests(unittest.TestCase):
    def test_a_complete_response_parses(self):
        token = parse_token_response(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
            },
            provider="p",
            now=1000.0,
        )

        self.assertEqual(token.access_token, "at")
        self.assertEqual(token.refresh_token, "rt")
        self.assertEqual(token.expires_at, 4600.0)
        self.assertEqual(token.provider, "p")

    def test_a_missing_access_token_is_refused(self):
        with self.assertRaises(OAuthError):
            parse_token_response({"refresh_token": "rt"})

    def test_a_provider_error_is_raised_with_its_detail(self):
        with self.assertRaises(OAuthError) as caught:
            parse_token_response(
                {"error": "invalid_grant", "error_description": "expired"}
            )

        self.assertIn("invalid_grant", str(caught.exception))
        self.assertIn("expired", str(caught.exception))

    def test_a_non_object_response_is_refused(self):
        for payload in (None, [], "token"):
            with self.subTest(payload=payload), self.assertRaises(OAuthError):
                parse_token_response(payload)

    def test_an_absent_expiry_means_no_deadline(self):
        token = parse_token_response({"access_token": "at"})

        self.assertIsNone(token.expires_at)
        self.assertFalse(token.is_expired)

    def test_a_nonsense_expiry_is_ignored(self):
        token = parse_token_response({"access_token": "at", "expires_in": True})

        self.assertIsNone(token.expires_at)


class TokenLifetimeTests(unittest.TestCase):
    def _token(self, expires_at, *, now, refresh="rt"):
        return OAuthToken(
            access_token="at",
            refresh_token=refresh,
            expires_at=expires_at,
            provider="p",
            _clock=lambda: now,
        )

    def test_a_future_token_is_neither_expired_nor_due(self):
        token = self._token(10_000.0, now=1000.0)

        self.assertFalse(token.is_expired)
        self.assertFalse(token.needs_refresh)

    def test_a_token_inside_the_margin_needs_refreshing(self):
        token = self._token(1000.0, now=1000.0 - REFRESH_MARGIN_SECONDS + 1)

        self.assertFalse(token.is_expired)
        self.assertTrue(token.needs_refresh)

    def test_an_expired_token_with_a_refresh_is_still_usable(self):
        token = self._token(100.0, now=1000.0)

        self.assertTrue(token.is_expired)
        self.assertTrue(token.is_usable)

    def test_an_expired_token_without_a_refresh_is_not_usable(self):
        token = self._token(100.0, now=1000.0, refresh=None)

        self.assertFalse(token.is_usable)

    def test_a_token_is_redacted(self):
        redacted = OAuthToken(access_token="secret-value-tail").redacted()

        self.assertNotIn("secret-value", redacted)
        self.assertIn("tail", redacted)

    def test_an_empty_access_token_is_refused(self):
        with self.assertRaises(OAuthError):
            OAuthToken(access_token="  ")


class RequestBodyTests(unittest.TestCase):
    def test_the_exchange_carries_the_verifier(self):
        body = exchange_body(
            CLIENT,
            code="c",
            redirect_uri="http://127.0.0.1:9/callback",
            verifier="v",
        )

        self.assertEqual(body["grant_type"], "authorization_code")
        self.assertEqual(body["code_verifier"], "v")

    def test_the_refresh_carries_the_refresh_token(self):
        body = refresh_body(CLIENT, refresh_token="rt")

        self.assertEqual(body["grant_type"], "refresh_token")
        self.assertEqual(body["refresh_token"], "rt")


class TokenStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)
        self.path = self.root / "tokens.json"

    def test_a_token_round_trips(self):
        token = OAuthToken(
            access_token="at",
            refresh_token="rt",
            expires_at=99.0,
            provider="p",
        )

        store_token(token, self.path)

        self.assertEqual(load_tokens(self.path)["p"], token)

    def test_a_second_provider_does_not_replace_the_first(self):
        store_token(OAuthToken(access_token="a", provider="one"), self.path)
        store_token(OAuthToken(access_token="b", provider="two"), self.path)

        self.assertEqual(sorted(load_tokens(self.path)), ["one", "two"])

    def test_the_file_is_private(self):
        store_token(OAuthToken(access_token="a", provider="p"), self.path)

        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_forgetting_removes_only_that_provider(self):
        store_token(OAuthToken(access_token="a", provider="one"), self.path)
        store_token(OAuthToken(access_token="b", provider="two"), self.path)

        self.assertTrue(forget_token("one", self.path))

        self.assertEqual(sorted(load_tokens(self.path)), ["two"])

    def test_forgetting_something_absent_reports_false(self):
        self.assertFalse(forget_token("nobody", self.path))

    def test_a_token_without_a_provider_is_refused(self):
        with self.assertRaises(OAuthError):
            store_token(OAuthToken(access_token="a"), self.path)

    def test_a_corrupt_store_is_ignored_rather_than_raised(self):
        self.path.write_text("{not json", encoding="utf-8")

        self.assertEqual(load_tokens(self.path), {})

    def test_a_missing_store_is_empty(self):
        self.assertEqual(load_tokens(self.root / "absent.json"), {})

    def test_entries_without_an_access_token_are_skipped(self):
        raw = encode_tokens({"p": OAuthToken(access_token="a", provider="p")})
        broken = raw.replace('"access_token": "a"', '"access_token": ""')

        self.assertEqual(parse_tokens(broken), {})

    def test_a_wrong_version_is_refused(self):
        with self.assertRaises(OAuthError):
            parse_tokens('{"version": 99, "tokens": {}}')

    def test_saving_nothing_is_valid(self):
        self.assertTrue(save_tokens({}, self.path))
        self.assertEqual(load_tokens(self.path), {})


class AuthoriseFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_callback_server_reports_the_state_it_expected(self):
        server = CallbackServer(expected_state="s1")
        await server.start()
        self.addAsyncCleanup(server.stop)

        self.assertTrue(server.redirect_uri.startswith("http://127.0.0.1:"))
        self.assertIn("/callback", server.redirect_uri)

    async def test_a_browser_that_never_returns_times_out(self):
        with self.assertRaises(OAuthError) as caught:
            await authorise(CLIENT, open_browser=lambda _target: None, timeout=0.2)

        self.assertIn("time", str(caught.exception))

    async def test_a_non_client_is_rejected(self):
        with self.assertRaises(OAuthError):
            await authorise("not a client")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
