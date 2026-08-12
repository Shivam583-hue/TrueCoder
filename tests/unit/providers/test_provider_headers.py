"""A provider may need extra request headers, but never the one that authenticates."""

from __future__ import annotations

import json
import unittest

from truecoder.providers import (
    CredentialError,
    Provider,
    ProviderConfigError,
    parse_providers,
)
from truecoder.providers.models import MAX_HEADER_VALUE_CHARACTERS, MAX_HEADERS


def _configured(headers: object) -> str:
    return json.dumps(
        {
            "version": 1,
            "providers": [
                {
                    "name": "acme",
                    "base_url": "https://api.acme.invalid/v1",
                    "headers": headers,
                }
            ],
        }
    )


class ProviderHeaderTests(unittest.TestCase):
    def test_a_provider_without_headers_has_none(self):
        self.assertEqual(Provider(name="acme").headers, {})

    def test_headers_survive_as_a_mapping(self):
        provider = Provider(name="acme", header_pairs=(("anthropic-beta", "one,two"),))

        self.assertEqual(provider.headers, {"anthropic-beta": "one,two"})

    def test_the_authorisation_header_cannot_be_configured(self):
        with self.assertRaises(CredentialError) as caught:
            Provider(name="acme", header_pairs=(("Authorization", "Bearer sneaky"),))

        self.assertIn("credential", str(caught.exception))

    def test_a_repeated_header_is_refused(self):
        with self.assertRaises(CredentialError):
            Provider(name="acme", header_pairs=(("X-One", "a"), ("x-one", "b")))

    def test_too_many_headers_are_refused(self):
        pairs = tuple((f"x-{index}", "v") for index in range(MAX_HEADERS + 1))

        with self.assertRaises(CredentialError):
            Provider(name="acme", header_pairs=pairs)

    def test_an_oversized_value_is_refused(self):
        with self.assertRaises(CredentialError):
            Provider(
                name="acme",
                header_pairs=(("x-one", "v" * (MAX_HEADER_VALUE_CHARACTERS + 1)),),
            )

    def test_a_nameless_header_is_refused(self):
        with self.assertRaises(CredentialError):
            Provider(name="acme", header_pairs=((" ", "value"),))


class ConfiguredHeaderTests(unittest.TestCase):
    def test_configured_headers_reach_the_provider(self):
        providers = parse_providers(_configured({"anthropic-beta": "context-1m"}))

        self.assertEqual(providers[0].headers, {"anthropic-beta": "context-1m"})

    def test_no_headers_key_is_fine(self):
        raw = json.dumps(
            {"version": 1, "providers": [{"name": "acme"}]},
        )

        self.assertEqual(parse_providers(raw)[0].headers, {})

    def test_headers_must_be_an_object(self):
        with self.assertRaises(ProviderConfigError):
            parse_providers(_configured(["anthropic-beta"]))

    def test_a_configured_authorisation_header_fails_closed(self):
        with self.assertRaises(ProviderConfigError) as caught:
            parse_providers(_configured({"authorization": "Bearer sneaky"}))

        self.assertIn("acme", str(caught.exception))

    def test_headers_are_ordered_for_a_stable_file(self):
        providers = parse_providers(_configured({"b": "2", "a": "1"}))

        self.assertEqual(providers[0].header_pairs, (("a", "1"), ("b", "2")))


if __name__ == "__main__":
    unittest.main()
