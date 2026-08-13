"""A model chosen once must still be chosen after a restart."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from truecoder.providers.catalog import CatalogSlice
from truecoder.providers.models import (
    ModelInfo,
    Provider,
    resolve_settings,
    settings_from_environment,
)
from truecoder.providers.store import (
    SettingsError,
    StoredSelection,
    encode_selection,
    load_selection,
    parse_selection,
    save_selection,
)


class ParseTests(unittest.TestCase):
    def test_a_stored_selection_parses(self):
        selection = parse_selection(
            json.dumps(
                {
                    "version": 1,
                    "model": "openai/gpt-5",
                    "provider": "default",
                    "reasoning_effort": "high",
                }
            )
        )

        self.assertEqual(selection.model, "openai/gpt-5")
        self.assertEqual(selection.provider, "default")
        self.assertEqual(selection.reasoning_effort, "high")

    def test_an_empty_selection_is_valid(self):
        self.assertTrue(parse_selection(json.dumps({"version": 1})).is_empty)

    def test_an_unknown_field_is_refused(self):
        with self.assertRaises(SettingsError):
            parse_selection(json.dumps({"version": 1, "surprise": True}))

    def test_a_wrong_version_is_refused(self):
        with self.assertRaises(SettingsError):
            parse_selection(json.dumps({"version": 2}))

    def test_invalid_json_is_refused(self):
        with self.assertRaises(SettingsError):
            parse_selection("{not json")

    def test_a_non_object_is_refused(self):
        with self.assertRaises(SettingsError):
            parse_selection(json.dumps([1, 2]))

    def test_a_non_text_model_is_refused(self):
        with self.assertRaises(SettingsError):
            parse_selection(json.dumps({"version": 1, "model": 5}))

    def test_an_unknown_reasoning_effort_is_refused(self):
        with self.assertRaises(SettingsError):
            parse_selection(json.dumps({"version": 1, "reasoning_effort": "turbo"}))

    def test_an_oversized_value_is_refused(self):
        with self.assertRaises(SettingsError):
            parse_selection(json.dumps({"version": 1, "model": "x" * 500}))

    def test_a_blank_model_becomes_nothing(self):
        self.assertIsNone(
            parse_selection(json.dumps({"version": 1, "model": "  "})).model
        )


class RoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)
        self.path = self.root / "settings.json"

    def test_a_selection_survives_a_write_and_read(self):
        selection = StoredSelection(
            model="anthropic/claude-opus-5",
            provider="p",
            reasoning_effort="high",
        )

        self.assertTrue(save_selection(selection, self.path))

        self.assertEqual(load_selection(self.path), selection)

    def test_encoding_omits_what_was_never_chosen(self):
        encoded = json.loads(encode_selection(StoredSelection(model="m")))

        self.assertNotIn("provider", encoded)
        self.assertEqual(encoded["model"], "m")

    def test_a_missing_file_yields_nothing_chosen(self):
        self.assertTrue(load_selection(self.root / "absent.json").is_empty)

    def test_a_corrupt_file_yields_nothing_rather_than_raising(self):
        self.path.write_text("{not json", encoding="utf-8")

        self.assertTrue(load_selection(self.path).is_empty)

    def test_saving_replaces_the_previous_choice(self):
        save_selection(StoredSelection(model="first"), self.path)
        save_selection(StoredSelection(model="second"), self.path)

        self.assertEqual(load_selection(self.path).model, "second")

    def test_an_unwritable_location_reports_failure(self):
        blocked = self.root / "file.txt"
        blocked.write_text("x", encoding="utf-8")

        self.assertFalse(save_selection(StoredSelection(model="m"), blocked / "s.json"))


class ResolutionTests(unittest.TestCase):
    def test_a_fresh_install_can_resolve_before_a_model_is_selected(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = settings_from_environment()

        self.assertEqual(settings.model, "")
        self.assertFalse(settings.has_model)

    def test_a_stored_model_wins_over_the_environment(self):
        with patch.dict(
            "os.environ", {"MODEL": "from-env", "API_KEY": "k"}, clear=True
        ):
            settings = settings_from_environment(stored_model="from-store")

        self.assertEqual(settings.model, "from-store")

    def test_the_environment_is_used_when_nothing_was_stored(self):
        with patch.dict(
            "os.environ", {"MODEL": "from-env", "API_KEY": "k"}, clear=True
        ):
            settings = settings_from_environment(stored_model=None)

        self.assertEqual(settings.model, "from-env")

    def test_a_blank_stored_model_falls_back(self):
        with patch.dict(
            "os.environ", {"MODEL": "from-env", "API_KEY": "k"}, clear=True
        ):
            settings = settings_from_environment(stored_model="   ")

        self.assertEqual(settings.model, "from-env")

    def test_resolution_prefers_a_stored_model_and_a_stored_token(self):
        from truecoder.providers.oauth import OAuthToken

        stored = StoredSelection(model="stored-model")
        token = OAuthToken(access_token="at", provider="openai")

        with (
            patch.dict(
                "os.environ", {"MODEL": "env-model", "API_KEY": "k"}, clear=True
            ),
            patch("truecoder.providers.store.load_selection", return_value=stored),
            patch(
                "truecoder.providers.tokens.load_tokens",
                return_value={"openai": token},
            ),
        ):
            settings = resolve_settings()

        self.assertEqual(settings.model, "stored-model")
        self.assertEqual(settings.credential, token)

    def test_an_expired_token_without_refresh_is_not_used(self):
        from truecoder.providers.oauth import OAuthToken

        dead = OAuthToken(
            access_token="at",
            provider="default",
            expires_at=1.0,
            _clock=lambda: 10**9,
        )

        with (
            patch.dict("os.environ", {"MODEL": "m", "API_KEY": "k"}, clear=True),
            patch(
                "truecoder.providers.store.load_selection",
                return_value=StoredSelection(),
            ),
            patch(
                "truecoder.providers.tokens.load_tokens",
                return_value={"default": dead},
            ),
        ):
            settings = resolve_settings()

        self.assertEqual(settings.credential.kind, "api-key")

    def test_a_stored_openai_selection_resolves_the_built_in_provider(self):
        from truecoder.providers.oauth import OAuthToken

        token = OAuthToken(access_token="at", provider="openai")
        with (
            patch.dict(
                "os.environ",
                {
                    "MODEL": "router/model",
                    "BASE_URL": "https://router.invalid/v1",
                },
                clear=True,
            ),
            patch(
                "truecoder.providers.store.load_selection",
                return_value=StoredSelection(model="gpt-5.2", provider="openai"),
            ),
            patch("truecoder.providers.configuration.load_providers", return_value=()),
            patch(
                "truecoder.providers.tokens.load_tokens",
                return_value={"openai": token},
            ),
        ):
            settings = resolve_settings()

        self.assertEqual(settings.provider.name, "openai")
        self.assertEqual(settings.provider.wire_api, "responses")
        self.assertEqual(settings.credential, token)

    def test_a_legacy_default_key_is_reused_for_direct_openai(self):
        from truecoder.providers import ApiKey

        key = ApiKey("sk-legacy")
        with (
            patch.dict("os.environ", {"MODEL": "gpt-5.2"}, clear=True),
            patch(
                "truecoder.providers.store.load_selection",
                return_value=StoredSelection(),
            ),
            patch("truecoder.providers.configuration.load_providers", return_value=()),
            patch("truecoder.providers.tokens.load_tokens", return_value={}),
            patch(
                "truecoder.providers.keys.load_keys",
                return_value={"default": key},
            ),
        ):
            settings = resolve_settings()

        self.assertEqual(settings.provider.name, "openai")
        self.assertEqual(settings.credential, key)

    def test_a_stored_model_restores_its_transport_override(self):
        provider = Provider(
            name="gateway",
            base_url="https://gateway.invalid/v1",
        )
        model = ModelInfo(
            identifier="claude",
            provider="gateway",
            base_url="https://gateway.invalid/anthropic/v1",
            adapter="anthropic",
        )
        directory = (CatalogSlice(provider, (model,)),)

        with (
            patch.dict("os.environ", {"MODEL": "fallback"}, clear=True),
            patch(
                "truecoder.providers.store.load_selection",
                return_value=StoredSelection(model="claude", provider="gateway"),
            ),
            patch("truecoder.providers.configuration.load_providers", return_value=()),
            patch(
                "truecoder.providers.catalog.read_models_dev_cache",
                return_value=directory,
            ),
            patch("truecoder.providers.tokens.load_tokens", return_value={}),
            patch("truecoder.providers.keys.load_keys", return_value={}),
        ):
            settings = resolve_settings()

        self.assertEqual(settings.provider.name, "gateway")
        self.assertEqual(settings.provider.adapter, "anthropic")
        self.assertEqual(
            settings.provider.base_url,
            "https://gateway.invalid/anthropic/v1",
        )


if __name__ == "__main__":
    unittest.main()
