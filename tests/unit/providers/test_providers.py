"""Credentials, the active model, and an untrusted provider model list."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from truecoder.providers import (
    MAX_MODELS,
    ApiKey,
    CredentialError,
    ModelInfo,
    Provider,
    SessionSettings,
    decode_cache,
    encode_cache,
    parse_models,
    read_cache,
    settings_from_environment,
    write_cache,
)


def _settings(**overrides) -> SessionSettings:
    values = {
        "provider": Provider(name="default", base_url=None),
        "credential": ApiKey("sk-secret-1234"),
        "model": "gpt-4",
    }
    values.update(overrides)
    return SessionSettings(**values)  # type: ignore[arg-type]


class CredentialTests(unittest.TestCase):
    def test_an_api_key_becomes_client_options(self):
        self.assertEqual(ApiKey("abc").client_options(), {"api_key": "abc"})

    def test_an_empty_api_key_is_refused(self):
        for value in ("", "   "):
            with self.subTest(value=value), self.assertRaises(CredentialError):
                ApiKey(value)

    def test_a_key_is_redacted_rather_than_shown(self):
        redacted = ApiKey("sk-supersecret-tail").redacted()

        self.assertNotIn("supersecret", redacted)
        self.assertIn("tail", redacted)


class ProviderTests(unittest.TestCase):
    def test_the_models_url_is_derived_from_the_base_url(self):
        provider = Provider(name="openrouter", base_url="https://x.invalid/api/v1")

        self.assertEqual(provider.models_url, "https://x.invalid/api/v1/models")

    def test_a_trailing_slash_does_not_double_up(self):
        provider = Provider(name="p", base_url="https://x.invalid/v1/")

        self.assertEqual(provider.models_url, "https://x.invalid/v1/models")

    def test_a_provider_needs_a_name(self):
        with self.assertRaises(CredentialError):
            Provider(name="  ")

    def test_an_oauth_catalog_can_override_the_key_catalog(self):
        from truecoder.providers.oauth import OAuthClient, OAuthToken

        oauth = OAuthClient(
            client_id="client",
            authorize_url="https://x.invalid/authorize",
            token_url="https://x.invalid/token",
            models_url="https://subscription.invalid/models",
        )
        provider = Provider(
            name="p",
            base_url="https://keys.invalid/v1",
            oauth=oauth,
        )

        self.assertEqual(
            provider.models_url_for(OAuthToken(access_token="at")),
            "https://subscription.invalid/models",
        )
        self.assertEqual(
            provider.models_url_for(ApiKey("sk")),
            "https://keys.invalid/v1/models",
        )


class SessionSettingsTests(unittest.TestCase):
    def test_selecting_a_model_updates_the_active_one(self):
        settings = _settings()

        settings.select_model("  claude-opus-5  ")

        self.assertEqual(settings.model, "claude-opus-5")

    def test_an_empty_model_is_refused(self):
        with self.assertRaises(CredentialError):
            _settings().select_model("   ")

    def test_changing_the_model_never_invalidates_the_connection(self):
        settings = _settings()
        invalidations: list[int] = []
        settings.on_connection_change(lambda: invalidations.append(1))

        settings.select_model("another-model")

        self.assertEqual(invalidations, [])

    def test_changing_the_provider_invalidates_the_connection(self):
        settings = _settings()
        invalidations: list[int] = []
        settings.on_connection_change(lambda: invalidations.append(1))

        settings.use(Provider(name="other", base_url="https://y.invalid"), ApiKey("k"))

        self.assertEqual(len(invalidations), 1)

    def test_reusing_the_same_connection_does_not_invalidate(self):
        settings = _settings()
        invalidations: list[int] = []
        settings.on_connection_change(lambda: invalidations.append(1))

        settings.use(settings.provider, settings.credential)

        self.assertEqual(invalidations, [])

    def test_a_non_callable_listener_is_rejected(self):
        with self.assertRaises(TypeError):
            _settings().on_connection_change("not callable")


class EnvironmentTests(unittest.TestCase):
    def test_the_environment_resolves_into_settings(self):
        with patch.dict(
            "os.environ",
            {"MODEL": "m", "API_KEY": "k", "BASE_URL": "https://x.invalid/v1"},
            clear=True,
        ):
            settings = settings_from_environment()

        self.assertEqual(settings.model, "m")
        self.assertEqual(settings.provider.base_url, "https://x.invalid/v1")
        assert settings.credential is not None

    def test_openai_is_the_direct_default_without_an_endpoint_override(self):
        with patch.dict(
            "os.environ",
            {"MODEL": "gpt-5.2"},
            clear=True,
        ):
            settings = settings_from_environment()

        self.assertEqual(settings.provider.label, "OpenAI")
        self.assertEqual(settings.provider.name, "openai")
        self.assertIsNotNone(settings.provider.oauth)

    def test_a_custom_endpoint_remains_a_key_only_provider(self):
        with patch.dict(
            "os.environ",
            {"MODEL": "openai/gpt-5.2", "BASE_URL": "https://router.invalid/v1"},
            clear=True,
        ):
            settings = settings_from_environment()

        self.assertIsNone(settings.provider.oauth)

    def test_an_openrouter_endpoint_gets_its_real_provider_identity(self):
        with patch.dict(
            "os.environ",
            {
                "MODEL": "openai/gpt-5.2",
                "BASE_URL": "https://openrouter.ai/api/v1",
                "OPENROUTER_API_KEY": "or-key",
            },
            clear=True,
        ):
            settings = settings_from_environment()

        self.assertEqual(settings.provider.name, "openrouter")
        self.assertEqual(settings.provider.label, "OpenRouter")
        self.assertEqual(settings.credential, ApiKey("or-key"))

    def test_an_unknown_endpoint_is_visible_as_a_custom_provider(self):
        with patch.dict(
            "os.environ",
            {"MODEL": "m", "BASE_URL": "https://router.invalid/v1"},
            clear=True,
        ):
            settings = settings_from_environment()

        self.assertEqual(settings.provider.name, "custom")
        self.assertEqual(settings.provider.label, "Custom provider")

    def test_a_missing_model_is_refused_by_name(self):
        with (
            patch.dict("os.environ", {"API_KEY": "k"}, clear=True),
            self.assertRaises(CredentialError) as caught,
        ):
            settings_from_environment()

        self.assertIn("MODEL", str(caught.exception))

    def test_a_missing_key_leaves_the_credential_unset(self):
        with patch.dict("os.environ", {"MODEL": "m"}, clear=True):
            settings = settings_from_environment()

        self.assertIsNone(settings.credential)


class ParseModelsTests(unittest.TestCase):
    def test_a_well_formed_listing_is_kept(self):
        models = parse_models(
            {"data": [{"id": "gpt-4", "name": "GPT 4", "context_length": 128000}]},
            "openai",
        )

        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].identifier, "gpt-4")
        self.assertEqual(models[0].context_window, 128000)
        self.assertEqual(models[0].context_label, "128K")

    def test_entries_without_an_id_are_skipped(self):
        models = parse_models({"data": [{"name": "x"}, {"id": "ok"}]}, "p")

        self.assertEqual([model.identifier for model in models], ["ok"])

    def test_duplicates_are_collapsed(self):
        models = parse_models({"data": [{"id": "a"}, {"id": "a"}]}, "p")

        self.assertEqual(len(models), 1)

    def test_the_listing_is_bounded(self):
        payload = {"data": [{"id": f"m{index}"} for index in range(MAX_MODELS + 50)]}

        self.assertEqual(len(parse_models(payload, "p")), MAX_MODELS)

    def test_a_malformed_payload_yields_nothing(self):
        for payload in (None, [], {"data": "many"}, {}, 7):
            with self.subTest(payload=payload):
                self.assertEqual(parse_models(payload, "p"), ())

    def test_a_nested_context_window_is_found(self):
        models = parse_models(
            {"data": [{"id": "m", "top_provider": {"context_length": 8192}}]},
            "p",
        )

        self.assertEqual(models[0].context_window, 8192)

    def test_an_absurd_context_window_is_ignored(self):
        models = parse_models(
            {"data": [{"id": "m", "context_length": 10**12}]},
            "p",
        )

        self.assertIsNone(models[0].context_window)

    def test_results_are_sorted_for_a_stable_picker(self):
        models = parse_models({"data": [{"id": "z"}, {"id": "a"}]}, "p")

        self.assertEqual([model.identifier for model in models], ["a", "z"])

    def test_a_codex_listing_uses_slugs_and_display_names(self):
        models = parse_models(
            {
                "models": [
                    {
                        "slug": "gpt-5.2-codex",
                        "display_name": "GPT-5.2-Codex",
                        "context_window": 400000,
                        "visibility": "list",
                    }
                ]
            },
            "openai",
        )

        self.assertEqual(models[0].identifier, "gpt-5.2-codex")
        self.assertEqual(models[0].display_name, "GPT-5.2-Codex")
        self.assertEqual(models[0].context_window, 400000)

    def test_a_hidden_codex_model_is_not_offered(self):
        models = parse_models(
            {
                "models": [
                    {"slug": "listed", "visibility": "list"},
                    {"slug": "hidden", "visibility": "hide"},
                ]
            },
            "openai",
        )

        self.assertEqual([model.identifier for model in models], ["listed"])


class MatchTests(unittest.TestCase):
    def test_a_query_matches_across_id_and_name(self):
        model = ModelInfo(identifier="anthropic/claude-opus", display_name="Opus")

        self.assertTrue(model.matches("claude"))
        self.assertTrue(model.matches("opus"))
        self.assertTrue(model.matches("anthropic opus"))
        self.assertFalse(model.matches("gemini"))

    def test_an_empty_query_matches_everything(self):
        self.assertTrue(ModelInfo(identifier="x").matches("  "))

    def test_a_model_has_an_unambiguous_provider_qualified_identity(self):
        model = ModelInfo(identifier="openai/gpt-5.2", provider="openrouter")

        self.assertEqual(
            model.qualified_identifier,
            "openrouter/openai/gpt-5.2",
        )


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def test_a_cache_round_trips(self):
        models = (ModelInfo(identifier="a", context_window=100),)

        decoded = decode_cache(encode_cache(models, fetched_at=1000.0), now=1001.0)

        self.assertEqual(decoded, models)

    def test_an_expired_cache_is_ignored(self):
        raw = encode_cache((ModelInfo(identifier="a"),), fetched_at=0.0)

        self.assertIsNone(decode_cache(raw, now=10**9))

    def test_a_corrupt_cache_is_ignored(self):
        for raw in ("{not json", "[]", json.dumps({"models": []})):
            with self.subTest(raw=raw):
                self.assertIsNone(decode_cache(raw, now=1.0))

    def test_writing_and_reading_a_cache_file(self):
        path = self.root / "models.json"
        models = (ModelInfo(identifier="a"),)

        write_cache(path, models)

        self.assertEqual(read_cache(path), models)

    def test_a_missing_cache_file_is_not_an_error(self):
        self.assertIsNone(read_cache(self.root / "absent.json"))

    def test_an_unwritable_cache_never_raises(self):
        write_cache(self.root / "nope" / "x" / "models.json", ())


if __name__ == "__main__":
    unittest.main()
