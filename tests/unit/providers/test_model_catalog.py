"""A catalog that spans providers must say which provider each model came from."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from truecoder.providers import (
    ApiKey,
    CatalogError,
    CatalogSlice,
    ModelInfo,
    Provider,
    bearer_token,
    catalog_path_for,
    catalog_problem,
    catalog_slug,
    fetch_models,
    load_catalog,
    merge_models,
    selectable_providers,
)
from truecoder.providers.catalog import EMPTY_CATALOG_REASON
from truecoder.providers.oauth import OAuthToken
from truecoder.providers.openai import OPENAI_CODEX_MODELS_URL, openai_provider

ACME = Provider(name="acme", base_url="https://api.acme.invalid/v1")
BRIO = Provider(name="brio", base_url="https://api.brio.invalid/v1")


class BearerTests(unittest.TestCase):
    def test_an_api_key_becomes_the_bearer(self):
        self.assertEqual(bearer_token(ApiKey("sk-1")), "sk-1")

    def test_an_oauth_token_becomes_the_bearer(self):
        token = OAuthToken(access_token="at-1", provider="acme")

        self.assertEqual(bearer_token(token), "at-1")

    def test_no_credential_yields_no_bearer(self):
        self.assertEqual(bearer_token(None), "")


class _CatalogResponse:
    status_code = 200
    content = b'{"models":[{"slug":"gpt-5.2","visibility":"list"}]}'


class _CatalogClient:
    def __init__(self) -> None:
        self.url = ""
        self.headers: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, url, *, headers):
        self.url = str(url)
        self.headers = headers
        return _CatalogResponse()


class FetchCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_oauth_uses_the_subscription_catalog_and_account_headers(self):
        client = _CatalogClient()
        token = OAuthToken(
            access_token="at-openai",
            provider="openai",
            metadata=(("ChatGPT-Account-Id", "acct-1"),),
        )

        with patch("httpx.AsyncClient", return_value=client):
            models = await fetch_models(openai_provider(), token)

        self.assertEqual(client.url, OPENAI_CODEX_MODELS_URL)
        self.assertEqual(client.headers["Authorization"], "Bearer at-openai")
        self.assertEqual(client.headers["ChatGPT-Account-Id"], "acct-1")
        self.assertEqual(client.headers["originator"], "truecoder")
        self.assertEqual(models[0].identifier, "gpt-5.2")


class SlugTests(unittest.TestCase):
    def test_a_slug_stays_readable(self):
        self.assertTrue(catalog_slug("openrouter").startswith("openrouter-"))

    def test_names_that_clean_up_the_same_never_collide(self):
        self.assertNotEqual(catalog_slug("a/b"), catalog_slug("a-b"))

    def test_each_provider_caches_to_its_own_file(self):
        self.assertNotEqual(catalog_path_for("acme"), catalog_path_for("brio"))

    def test_the_same_provider_always_lands_in_the_same_file(self):
        self.assertEqual(catalog_path_for("acme"), catalog_path_for("acme"))


class LoadCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_provider_is_asked(self):
        asked: list[str] = []

        async def listing(provider, credential, *, refresh=False):
            asked.append(provider.name)
            return (ModelInfo(identifier="m-1", provider=provider.name),)

        with patch("truecoder.providers.catalog.load_models", side_effect=listing):
            slices = await load_catalog((ACME, BRIO), {})

        self.assertEqual(asked, ["acme", "brio"])
        self.assertEqual(len(slices), 2)

    async def test_each_provider_is_asked_with_its_own_credential(self):
        seen: dict[str, object] = {}

        async def listing(provider, credential, *, refresh=False):
            seen[provider.name] = credential
            return (ModelInfo(identifier="m-1", provider=provider.name),)

        with patch("truecoder.providers.catalog.load_models", side_effect=listing):
            await load_catalog((ACME, BRIO), {"acme": ApiKey("sk-acme")})

        self.assertEqual(seen["acme"], ApiKey("sk-acme"))
        self.assertIsNone(seen["brio"])

    async def test_one_unreachable_provider_never_hides_the_others(self):
        async def listing(provider, credential, *, refresh=False):
            if provider.name == "acme":
                raise CatalogError("the provider returned 401")
            return (ModelInfo(identifier="brio/one", provider="brio"),)

        with patch("truecoder.providers.catalog.load_models", side_effect=listing):
            slices = await load_catalog((ACME, BRIO), {})

        self.assertEqual(slices[0].reason, "the provider returned 401")
        self.assertEqual(slices[0].models, ())
        self.assertEqual(len(slices[1].models), 1)
        self.assertIsNone(catalog_problem(slices))

    async def test_a_provider_that_lists_nothing_says_so(self):
        async def listing(provider, credential, *, refresh=False):
            return ()

        with patch("truecoder.providers.catalog.load_models", side_effect=listing):
            slices = await load_catalog((ACME,), {})

        self.assertEqual(slices[0].reason, EMPTY_CATALOG_REASON)


class MergeTests(unittest.TestCase):
    def test_models_are_grouped_by_provider_then_identifier(self):
        slices = (
            CatalogSlice(BRIO, (ModelInfo(identifier="z", provider="brio"),)),
            CatalogSlice(
                ACME,
                (
                    ModelInfo(identifier="b", provider="acme"),
                    ModelInfo(identifier="a", provider="acme"),
                ),
            ),
        )

        merged = merge_models(slices)

        self.assertEqual(
            [(model.provider, model.identifier) for model in merged],
            [("acme", "a"), ("acme", "b"), ("brio", "z")],
        )

    def test_nothing_anywhere_reports_the_only_reason(self):
        slices = (CatalogSlice(ACME, (), "the provider returned 401"),)

        self.assertEqual(catalog_problem(slices), "the provider returned 401")

    def test_nothing_anywhere_names_each_provider_when_several_failed(self):
        slices = (
            CatalogSlice(ACME, (), "returned 401"),
            CatalogSlice(BRIO, (), "returned 500"),
        )

        problem = catalog_problem(slices)

        self.assertIn("acme: returned 401", str(problem))
        self.assertIn("brio: returned 500", str(problem))

    def test_a_query_matches_the_provider_name(self):
        model = ModelInfo(identifier="one", provider="openrouter")

        self.assertTrue(model.matches("openrouter"))
        self.assertFalse(model.matches("elsewhere"))


class SelectableProviderTests(unittest.TestCase):
    def test_the_active_provider_is_offered_when_nothing_is_configured(self):
        with patch(
            "truecoder.providers.configuration.load_providers",
            return_value=(),
        ):
            providers = selectable_providers(ACME)

        self.assertEqual(providers[0], ACME)
        self.assertEqual(providers[1].name, "openai")

    def test_the_active_provider_is_never_listed_twice(self):
        with patch(
            "truecoder.providers.configuration.load_providers",
            return_value=(ACME, BRIO),
        ):
            providers = selectable_providers(ACME)

        self.assertEqual(providers[:2], (ACME, BRIO))
        self.assertEqual(providers[2].name, "openai")

    def test_an_unconfigured_active_provider_leads_the_list(self):
        with patch(
            "truecoder.providers.configuration.load_providers",
            return_value=(BRIO,),
        ):
            providers = selectable_providers(ACME)

        self.assertEqual(providers[:2], (ACME, BRIO))
        self.assertEqual(providers[2].name, "openai")

    def test_the_built_in_is_not_added_twice(self):
        from truecoder.providers.openai import openai_provider

        active = openai_provider()
        with patch(
            "truecoder.providers.configuration.load_providers",
            return_value=(),
        ):
            providers = selectable_providers(active)

        self.assertEqual(providers, (active,))


if __name__ == "__main__":
    unittest.main()
