"""A typed API key is private on disk and outranks the environment."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers.platforms import requires_posix_permissions
from truecoder.providers.keys import (
    MAX_KEY_CHARACTERS,
    forget_key,
    load_keys,
    parse_keys,
    store_key,
)
from truecoder.providers.models import ApiKey, CredentialError, resolve_settings
from truecoder.providers.store import StoredSelection


class ParseTests(unittest.TestCase):
    def test_a_stored_key_parses(self):
        raw = json.dumps({"version": 1, "keys": {"acme": "sk-secret"}})

        self.assertEqual(parse_keys(raw)["acme"], ApiKey("sk-secret"))

    def test_no_keys_is_valid(self):
        self.assertEqual(parse_keys(json.dumps({"version": 1})), {})

    def test_a_wrong_version_is_refused(self):
        with self.assertRaises(CredentialError):
            parse_keys(json.dumps({"version": 2, "keys": {}}))

    def test_invalid_json_is_refused(self):
        with self.assertRaises(CredentialError):
            parse_keys("{not json")

    def test_an_empty_key_is_skipped_rather_than_stored(self):
        raw = json.dumps({"version": 1, "keys": {"acme": "   ", "other": "sk-ok"}})

        self.assertEqual(list(parse_keys(raw)), ["other"])

    def test_an_overlong_key_is_refused(self):
        raw = json.dumps(
            {"version": 1, "keys": {"acme": "s" * (MAX_KEY_CHARACTERS + 1)}}
        )

        with self.assertRaises(CredentialError):
            parse_keys(raw)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name).resolve() / "keys.json"
        self.addCleanup(self._directory.cleanup)

    def test_a_key_round_trips(self):
        self.assertTrue(store_key("acme", ApiKey("sk-secret"), self.path))

        self.assertEqual(load_keys(self.path)["acme"], ApiKey("sk-secret"))

    def test_a_second_provider_does_not_replace_the_first(self):
        store_key("acme", ApiKey("sk-one"), self.path)
        store_key("other", ApiKey("sk-two"), self.path)

        self.assertEqual(sorted(load_keys(self.path)), ["acme", "other"])

    def test_storing_the_same_provider_replaces_its_key(self):
        store_key("acme", ApiKey("sk-old"), self.path)
        store_key("acme", ApiKey("sk-new"), self.path)

        self.assertEqual(load_keys(self.path)["acme"], ApiKey("sk-new"))

    def test_a_key_can_be_forgotten(self):
        store_key("acme", ApiKey("sk-secret"), self.path)

        self.assertTrue(forget_key("acme", self.path))
        self.assertEqual(load_keys(self.path), {})

    def test_forgetting_an_absent_provider_reports_nothing_to_do(self):
        self.assertFalse(forget_key("absent", self.path))

    def test_a_missing_file_means_no_keys(self):
        self.assertEqual(load_keys(self.path), {})

    def test_a_broken_file_is_ignored_rather_than_raised(self):
        self.path.write_text("{not json", encoding="utf-8")

        self.assertEqual(load_keys(self.path), {})

    def test_a_key_needs_a_provider(self):
        with self.assertRaises(CredentialError):
            store_key("  ", ApiKey("sk-secret"), self.path)

    @requires_posix_permissions
    def test_the_file_is_private_to_this_user(self):
        store_key("acme", ApiKey("sk-secret"), self.path)

        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_the_key_never_appears_in_its_own_redaction(self):
        self.assertNotIn("sk-secret", ApiKey("sk-secret").redacted())


class ResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def _resolve(self, *, stored_key: str | None, environment: str | None):
        keys = {"default": ApiKey(stored_key)} if stored_key else {}
        variables = {"MODEL": "a/model"}
        if environment:
            variables["API_KEY"] = environment

        with (
            patch.dict(os.environ, variables, clear=True),
            patch(
                "truecoder.providers.store.load_selection",
                return_value=StoredSelection(),
            ),
            patch("truecoder.providers.configuration.load_providers", return_value=()),
            patch("truecoder.providers.keys.load_keys", return_value=keys),
            patch("truecoder.providers.tokens.load_tokens", return_value={}),
        ):
            return resolve_settings()

    def test_a_stored_key_is_used_when_the_environment_has_none(self):
        settings = self._resolve(stored_key="sk-stored", environment=None)

        self.assertEqual(settings.credential, ApiKey("sk-stored"))

    def test_a_stored_key_outranks_the_environment(self):
        settings = self._resolve(stored_key="sk-stored", environment="sk-environment")

        self.assertEqual(settings.credential, ApiKey("sk-stored"))

    def test_the_environment_still_works_when_nothing_is_stored(self):
        settings = self._resolve(stored_key=None, environment="sk-environment")

        self.assertEqual(settings.credential, ApiKey("sk-environment"))

    def test_no_credential_at_all_resolves_to_none(self):
        settings = self._resolve(stored_key=None, environment=None)

        self.assertIsNone(settings.credential)


if __name__ == "__main__":
    unittest.main()
