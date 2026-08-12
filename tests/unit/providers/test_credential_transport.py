"""A subscription token travels with headers and an endpoint a key never needs."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from truecoder.providers import ApiKey, OAuthClient, OAuthError, OAuthToken
from truecoder.providers.oauth import (
    MAX_ACCOUNT_CHARACTERS,
    authorization_url,
    find_claim,
    generate_pkce,
    parse_token_response,
    token_claims,
)
from truecoder.providers.tokens import load_tokens, save_tokens

ACCOUNT = "acct_12345"


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii")
    return f"header.{body.rstrip('=')}.signature"


def _client(**overrides) -> OAuthClient:
    values = {
        "client_id": "client-123",
        "authorize_url": "https://provider.invalid/oauth/authorize",
        "token_url": "https://provider.invalid/oauth/token",
    }
    values.update(overrides)
    return OAuthClient(**values)


class ClaimTests(unittest.TestCase):
    def test_a_top_level_claim_is_found(self):
        claims = token_claims(_jwt({"account_id": ACCOUNT}))

        self.assertEqual(find_claim(claims, "account_id"), ACCOUNT)

    def test_a_nested_claim_is_found(self):
        claims = token_claims(
            _jwt({"https://api.invalid/auth": {"account_id": ACCOUNT}})
        )

        self.assertEqual(find_claim(claims, "account_id"), ACCOUNT)

    def test_a_missing_claim_is_empty(self):
        self.assertEqual(find_claim(token_claims(_jwt({"sub": "x"})), "account_id"), "")

    def test_a_malformed_token_never_raises(self):
        for value in (
            "",
            "not-a-jwt",
            "a.b",
            "a.!!!.c",
            None,
            5,
            "a." + "x" * 20000 + ".c",
        ):
            self.assertEqual(token_claims(value), {})

    def test_a_non_object_payload_is_ignored(self):
        body = base64.urlsafe_b64encode(b'"just a string"').decode("ascii").rstrip("=")

        self.assertEqual(token_claims(f"a.{body}.c"), {})

    def test_an_absurd_account_is_bounded(self):
        claims = token_claims(_jwt({"account_id": "x" * (MAX_ACCOUNT_CHARACTERS * 2)}))

        self.assertEqual(len(find_claim(claims, "account_id")), MAX_ACCOUNT_CHARACTERS)


class TokenTransportTests(unittest.TestCase):
    def test_a_plain_token_carries_nothing_extra(self):
        token = parse_token_response({"access_token": "at-1"}, provider="acme")

        self.assertEqual(token.request_headers(), {})
        self.assertIsNone(token.endpoint_override())

    def test_a_configured_account_becomes_a_header(self):
        client = _client(account_claim="account_id", account_header="X-Account-Id")

        token = parse_token_response(
            {"access_token": "at-1", "id_token": _jwt({"account_id": ACCOUNT})},
            provider="acme",
            client=client,
        )

        self.assertEqual(token.request_headers(), {"X-Account-Id": ACCOUNT})

    def test_the_access_token_is_read_when_the_id_token_has_nothing(self):
        client = _client(account_claim="account_id", account_header="X-Account-Id")

        token = parse_token_response(
            {
                "access_token": _jwt({"account_id": ACCOUNT}),
                "id_token": _jwt({"sub": "nothing"}),
            },
            provider="acme",
            client=client,
        )

        self.assertEqual(token.request_headers(), {"X-Account-Id": ACCOUNT})

    def test_an_unconfigured_account_is_never_guessed(self):
        token = parse_token_response(
            {"access_token": "at-1", "id_token": _jwt({"account_id": ACCOUNT})},
            provider="acme",
            client=_client(),
        )

        self.assertEqual(token.request_headers(), {})

    def test_a_subscription_endpoint_overrides_the_provider(self):
        client = _client(api_base_url="https://chat.invalid/backend/v1")

        token = parse_token_response(
            {"access_token": "at-1"}, provider="acme", client=client
        )

        self.assertEqual(token.endpoint_override(), "https://chat.invalid/backend/v1")

    def test_a_key_never_overrides_anything(self):
        key = ApiKey("sk-1")

        self.assertEqual(key.request_headers(), {})
        self.assertIsNone(key.endpoint_override())


class ClientValidationTests(unittest.TestCase):
    def test_a_claim_without_a_header_is_refused(self):
        with self.assertRaises(OAuthError):
            _client(account_claim="account_id")

    def test_a_header_without_a_claim_is_refused(self):
        with self.assertRaises(OAuthError):
            _client(account_header="X-Account-Id")

    def test_the_account_header_cannot_be_authorisation(self):
        with self.assertRaises(OAuthError):
            _client(account_claim="account_id", account_header="Authorization")

    def test_an_insecure_api_base_url_is_refused(self):
        with self.assertRaises(OAuthError):
            _client(api_base_url="http://chat.invalid/v1")

    def test_extra_parameters_reach_the_authorisation_url(self):
        client = _client(extra_parameters=(("originator", "truecoder"),))

        url = authorization_url(
            client,
            redirect_uri="http://127.0.0.1:1455/callback",
            pkce=generate_pkce(),
            state="state-1",
        )

        self.assertIn("originator=truecoder", url)

    def test_extra_parameters_never_overwrite_the_protocol(self):
        client = _client(extra_parameters=(("client_id", "impostor"),))

        url = authorization_url(
            client,
            redirect_uri="http://127.0.0.1:1455/callback",
            pkce=generate_pkce(),
            state="state-1",
        )

        self.assertIn("client_id=client-123", url)
        self.assertNotIn("impostor", url)


class TokenStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name).resolve() / "tokens.json"
        self.addCleanup(self._directory.cleanup)

    def test_headers_and_endpoint_survive_a_restart(self):
        token = OAuthToken(
            access_token="at-1",
            provider="acme",
            metadata=(("X-Account-Id", ACCOUNT),),
            endpoint="https://chat.invalid/backend/v1",
        )

        save_tokens({"acme": token}, self.path)
        restored = load_tokens(self.path)["acme"]

        self.assertEqual(restored.request_headers(), {"X-Account-Id": ACCOUNT})
        self.assertEqual(
            restored.endpoint_override(), "https://chat.invalid/backend/v1"
        )

    def test_a_stored_authorisation_header_is_dropped(self):
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "tokens": {
                        "acme": {
                            "access_token": "at-1",
                            "metadata": {"Authorization": "Bearer sneaky", "X-Ok": "1"},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        restored = load_tokens(self.path)["acme"]

        self.assertEqual(restored.request_headers(), {"X-Ok": "1"})

    def test_malformed_metadata_is_ignored(self):
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "tokens": {
                        "acme": {
                            "access_token": "at-1",
                            "metadata": ["not", "a", "map"],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(load_tokens(self.path)["acme"].request_headers(), {})


if __name__ == "__main__":
    unittest.main()
