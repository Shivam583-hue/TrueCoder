"""Provider configuration is strict and fails closed like every other config."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from truecoder.providers.configuration import (
    MAX_PROVIDERS,
    ProviderConfigError,
    load_providers,
    parse_providers,
)

OAUTH = {
    "client_id": "cid",
    "authorize_url": "https://acme.invalid/oauth/authorize",
    "token_url": "https://acme.invalid/oauth/token",
    "scopes": ["read"],
}


def _config(**overrides) -> str:
    provider = {"name": "acme", "base_url": "https://api.acme.invalid/v1"}
    provider.update(overrides)
    return json.dumps({"version": 1, "providers": [provider]})


class ParseTests(unittest.TestCase):
    def test_a_minimal_provider_parses(self):
        providers = parse_providers(_config())

        self.assertEqual(providers[0].name, "acme")
        self.assertIsNone(providers[0].oauth)

    def test_an_oauth_client_parses(self):
        provider = parse_providers(
            _config(
                oauth={
                    **OAUTH,
                    "redirect_host": "localhost",
                    "redirect_path": "/auth/callback",
                }
            )
        )[0]

        assert provider.oauth is not None
        self.assertEqual(provider.oauth.client_id, "cid")
        self.assertEqual(provider.oauth.scopes, ("read",))
        self.assertEqual(provider.oauth.redirect_host, "localhost")
        self.assertEqual(provider.oauth.redirect_path, "/auth/callback")

    def test_no_providers_is_valid(self):
        self.assertEqual(parse_providers(json.dumps({"version": 1})), ())

    def test_an_unknown_root_field_is_refused(self):
        with self.assertRaises(ProviderConfigError):
            parse_providers(json.dumps({"version": 1, "extra": 1}))

    def test_an_unknown_provider_field_is_refused(self):
        with self.assertRaises(ProviderConfigError):
            parse_providers(_config(surprise=True))

    def test_an_unknown_oauth_field_is_refused(self):
        with self.assertRaises(ProviderConfigError):
            parse_providers(_config(oauth={**OAUTH, "secret": "no"}))

    def test_a_wrong_version_is_refused(self):
        with self.assertRaises(ProviderConfigError):
            parse_providers(json.dumps({"version": 2, "providers": []}))

    def test_invalid_json_is_refused(self):
        with self.assertRaises(ProviderConfigError):
            parse_providers("{not json")

    def test_a_provider_needs_a_name(self):
        with self.assertRaises(ProviderConfigError):
            parse_providers(json.dumps({"version": 1, "providers": [{}]}))

    def test_duplicate_names_are_refused(self):
        payload = json.dumps(
            {
                "version": 1,
                "providers": [{"name": "acme"}, {"name": "acme"}],
            }
        )

        with self.assertRaises(ProviderConfigError):
            parse_providers(payload)

    def test_too_many_providers_are_refused(self):
        payload = json.dumps(
            {
                "version": 1,
                "providers": [
                    {"name": f"p{index}"} for index in range(MAX_PROVIDERS + 1)
                ],
            }
        )

        with self.assertRaises(ProviderConfigError):
            parse_providers(payload)

    def test_an_insecure_oauth_endpoint_is_refused(self):
        with self.assertRaises(ProviderConfigError) as caught:
            parse_providers(
                _config(oauth={**OAUTH, "authorize_url": "http://acme.invalid/a"})
            )

        self.assertIn("https", str(caught.exception))

    def test_a_missing_oauth_client_id_is_refused(self):
        with self.assertRaises(ProviderConfigError):
            parse_providers(_config(oauth={**OAUTH, "client_id": ""}))

    def test_a_non_url_base_is_refused(self):
        with self.assertRaises(ProviderConfigError):
            parse_providers(_config(base_url="ftp://acme.invalid"))

    def test_too_many_scopes_are_refused(self):
        with self.assertRaises(ProviderConfigError):
            parse_providers(
                _config(oauth={**OAUTH, "scopes": [f"s{n}" for n in range(50)]})
            )


class LoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def test_a_missing_file_means_no_providers(self):
        self.assertEqual(load_providers(self.root / "absent.json"), ())

    def test_a_broken_file_is_ignored_rather_than_raised(self):
        path = self.root / "providers.json"
        path.write_text("{not json", encoding="utf-8")

        self.assertEqual(load_providers(path), ())

    def test_a_good_file_is_loaded(self):
        path = self.root / "providers.json"
        path.write_text(_config(oauth=OAUTH), encoding="utf-8")

        providers = load_providers(path)

        self.assertEqual(len(providers), 1)
        assert providers[0].oauth is not None


if __name__ == "__main__":
    unittest.main()
