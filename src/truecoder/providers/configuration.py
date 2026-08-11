from __future__ import annotations

import json
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

from platformdirs import user_config_path

from truecoder.providers.models import CredentialError, Provider
from truecoder.providers.oauth import OAuthClient, OAuthError

PROVIDERS_VERSION: Final = 1
MAX_CONFIG_BYTES: Final = 64 * 1024
MAX_PROVIDERS: Final = 16
MAX_SCOPES: Final = 32

_ROOT_FIELDS: Final = frozenset({"version", "providers"})
_PROVIDER_FIELDS: Final = frozenset({"name", "base_url", "oauth"})
_OAUTH_FIELDS: Final = frozenset(
    {"client_id", "authorize_url", "token_url", "scopes"}
)


class ProviderConfigError(ValueError):
    pass


def default_providers_config_path() -> Path:
    return user_config_path("truecoder", appauthor=False) / "providers.json"


def parse_providers(raw: str) -> tuple[Provider, ...]:
    if not isinstance(raw, str):
        raise ProviderConfigError("provider configuration must be text")
    if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ProviderConfigError("provider configuration is too large")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ProviderConfigError(
            f"provider configuration is not valid JSON: {error}"
        ) from None

    if not isinstance(payload, dict):
        raise ProviderConfigError("provider configuration must be a JSON object")

    unknown = set(payload) - _ROOT_FIELDS
    if unknown:
        raise ProviderConfigError(f"unknown configuration field(s): {sorted(unknown)}")
    if payload.get("version") != PROVIDERS_VERSION:
        raise ProviderConfigError(
            f"provider configuration version must be {PROVIDERS_VERSION}"
        )

    listed = payload.get("providers", [])
    if not isinstance(listed, list):
        raise ProviderConfigError("providers must be a list")
    if len(listed) > MAX_PROVIDERS:
        raise ProviderConfigError(f"at most {MAX_PROVIDERS} providers are supported")

    providers = tuple(_provider(entry) for entry in listed)
    names = [provider.name for provider in providers]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ProviderConfigError(f"duplicate provider name(s): {duplicates}")
    return providers


def load_providers(path: Path | None = None) -> tuple[Provider, ...]:
    target = path or default_providers_config_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return ()
    try:
        return parse_providers(raw)
    except ProviderConfigError:
        return ()


def selectable_providers(
    current: Provider,
    path: Path | None = None,
) -> tuple[Provider, ...]:
    configured = load_providers(path)
    if any(provider.name == current.name for provider in configured):
        return configured
    return (current, *configured)


def _provider(entry: object) -> Provider:
    if not isinstance(entry, dict):
        raise ProviderConfigError("each provider must be a JSON object")

    unknown = set(entry) - _PROVIDER_FIELDS
    if unknown:
        raise ProviderConfigError(f"unknown provider field(s): {sorted(unknown)}")

    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProviderConfigError("each provider needs a name")

    base_url = entry.get("base_url")
    if base_url is not None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ProviderConfigError(f"provider {name!r} base_url must be text")
        if urlparse(base_url).scheme not in {"http", "https"}:
            raise ProviderConfigError(
                f"provider {name!r} base_url must be an http or https URL"
            )

    try:
        return Provider(
            name=name.strip(),
            base_url=base_url,
            oauth=_oauth(name, entry.get("oauth")),
        )
    except (CredentialError, OAuthError) as error:
        raise ProviderConfigError(f"provider {name!r} is unusable: {error}") from None


def _oauth(name: str, value: object) -> OAuthClient | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProviderConfigError(f"provider {name!r} oauth must be an object")

    unknown = set(value) - _OAUTH_FIELDS
    if unknown:
        raise ProviderConfigError(
            f"provider {name!r} has unknown oauth field(s): {sorted(unknown)}"
        )

    scopes = value.get("scopes", [])
    if not isinstance(scopes, list):
        raise ProviderConfigError(f"provider {name!r} oauth scopes must be a list")
    if len(scopes) > MAX_SCOPES:
        raise ProviderConfigError(f"provider {name!r} declares too many oauth scopes")

    try:
        return OAuthClient(
            client_id=str(value.get("client_id", "")),
            authorize_url=str(value.get("authorize_url", "")),
            token_url=str(value.get("token_url", "")),
            scopes=tuple(str(scope) for scope in scopes),
        )
    except OAuthError as error:
        raise ProviderConfigError(
            f"provider {name!r} oauth is unusable: {error}"
        ) from None
