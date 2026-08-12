from __future__ import annotations

import unittest

from truecoder.providers.openai import (
    OPENAI_ACCOUNT_CLAIM,
    OPENAI_ACCOUNT_HEADER,
    OPENAI_API_BASE_URL,
    OPENAI_CODEX_BASE_URL,
    OPENAI_CODEX_CLIENT_ID,
    OPENAI_OAUTH_CLIENT,
    default_openai_provider,
    openai_provider,
)


class OpenAIProviderTests(unittest.TestCase):
    def test_the_public_codex_client_is_built_in(self):
        self.assertEqual(
            OPENAI_CODEX_CLIENT_ID,
            "app_EMoamEEZ73f0CkXaXp7hrann",
        )
        self.assertEqual(OPENAI_OAUTH_CLIENT.api_base_url, OPENAI_CODEX_BASE_URL)
        self.assertEqual(OPENAI_OAUTH_CLIENT.redirect_port, 1455)
        self.assertEqual(
            (
                OPENAI_OAUTH_CLIENT.redirect_host,
                OPENAI_OAUTH_CLIENT.redirect_path,
            ),
            ("localhost", "/auth/callback"),
        )
        self.assertFalse(OPENAI_OAUTH_CLIENT.supports_device_code)

    def test_the_login_requests_an_offline_token_and_codex_flow(self):
        self.assertIn("offline_access", OPENAI_OAUTH_CLIENT.scopes)
        parameters = dict(OPENAI_OAUTH_CLIENT.extra_parameters)
        self.assertEqual(parameters["codex_cli_simplified_flow"], "true")
        self.assertEqual(parameters["originator"], "truecoder")

    def test_the_account_claim_becomes_the_codex_request_header(self):
        self.assertEqual(OPENAI_OAUTH_CLIENT.account_claim, OPENAI_ACCOUNT_CLAIM)
        self.assertEqual(OPENAI_OAUTH_CLIENT.account_header, OPENAI_ACCOUNT_HEADER)

    def test_api_keys_use_the_public_openai_endpoint(self):
        provider = openai_provider()

        self.assertEqual(provider.name, "openai")
        self.assertEqual(provider.label, "OpenAI")
        self.assertEqual(provider.base_url, OPENAI_API_BASE_URL)

    def test_the_environment_default_keeps_legacy_storage_names(self):
        provider = default_openai_provider()

        self.assertEqual(provider.name, "default")
        self.assertEqual(provider.label, "OpenAI")
        self.assertIs(provider.oauth, OPENAI_OAUTH_CLIENT)


if __name__ == "__main__":
    unittest.main()
