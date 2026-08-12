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
MAX_EXTRA_PARAMETERS: Final = 16

_ROOT_FIELDS: Final = frozenset({"version", "providers"})
_PROVIDER_FIELDS: Final = frozenset(
    {
        "name",
        "base_url",
        "oauth",
        "headers",
        "display_name",
        "wire_api",
        "adapter",
        "env",
    }
)
_OAUTH_FIELDS: Final = frozenset(
    {
        "client_id",
        "authorize_url",
        "token_url",
        "scopes",
        "account_claim",
        "account_header",
        "api_base_url",
        "models_url",
        "extra_parameters",
        "redirect_port",
        "redirect_host",
        "redirect_path",
        "device_url",
        "device_token_url",
        "device_verification_url",
        "device_redirect_url",
    }
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
    from truecoder.providers.openai import is_openai_provider, openai_provider

    configured = load_providers(path)
    if any(provider.name == current.name for provider in configured):
        providers = configured
    else:
        providers = (current, *configured)

    if any(
        provider.name == "openai" or is_openai_provider(provider)
        for provider in providers
    ):
        return providers
    return (*providers, openai_provider())


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

    env = entry.get("env", [])
    if not isinstance(env, list):
        raise ProviderConfigError(f"provider {name!r} env must be a list")

    try:
        return Provider(
            name=name.strip(),
            base_url=base_url,
            oauth=_oauth(name, entry.get("oauth")),
            header_pairs=_headers(name, entry.get("headers")),
            display_name=str(entry.get("display_name", "")),
            wire_api=str(entry.get("wire_api", "chat")),
            adapter=str(entry.get("adapter", "openai-compatible")),
            env_names=tuple(str(item) for item in env),
        )
    except (CredentialError, OAuthError) as error:
        raise ProviderConfigError(f"provider {name!r} is unusable: {error}") from None


def _headers(name: str, value: object) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise ProviderConfigError(f"provider {name!r} headers must be an object")
    return tuple(
        (str(header), str(content)) for header, content in sorted(value.items())
    )


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

    extra = value.get("extra_parameters")
    if extra is not None and not isinstance(extra, dict):
        raise ProviderConfigError(
            f"provider {name!r} oauth extra_parameters must be an object"
        )
    if isinstance(extra, dict) and len(extra) > MAX_EXTRA_PARAMETERS:
        raise ProviderConfigError(
            f"provider {name!r} declares too many oauth extra_parameters"
        )

    port = value.get("redirect_port", 0)
    if isinstance(port, bool) or not isinstance(port, int):
        raise ProviderConfigError(
            f"provider {name!r} oauth redirect_port must be a whole number"
        )

    try:
        return OAuthClient(
            client_id=str(value.get("client_id", "")),
            authorize_url=str(value.get("authorize_url", "")),
            token_url=str(value.get("token_url", "")),
            scopes=tuple(str(scope) for scope in scopes),
            account_claim=str(value.get("account_claim", "")),
            account_header=str(value.get("account_header", "")),
            api_base_url=str(value.get("api_base_url", "")),
            models_url=str(value.get("models_url", "")),
            extra_parameters=tuple(
                (str(key), str(item)) for key, item in sorted((extra or {}).items())
            ),
            redirect_port=port,
            redirect_host=str(value.get("redirect_host", "127.0.0.1")),
            redirect_path=str(value.get("redirect_path", "/callback")),
            device_url=str(value.get("device_url", "")),
            device_token_url=str(value.get("device_token_url", "")),
            device_verification_url=str(value.get("device_verification_url", "")),
            device_redirect_url=str(value.get("device_redirect_url", "")),
        )
    except OAuthError as error:
        raise ProviderConfigError(
            f"provider {name!r} oauth is unusable: {error}"
        ) from None
