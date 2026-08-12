from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from truecoder.providers.oauth import OAuthClient

MAX_MODEL_ID_CHARACTERS: Final = 200
MAX_MODEL_NAME_CHARACTERS: Final = 120
DEFAULT_PROVIDER_NAME: Final = "default"
MAX_DISPLAY_NAME_CHARACTERS: Final = 60
MAX_HEADERS: Final = 16
MAX_ENV_NAMES: Final = 16
MAX_HEADER_NAME_CHARACTERS: Final = 64
MAX_HEADER_VALUE_CHARACTERS: Final = 1024
RESERVED_HEADERS: Final = frozenset({"authorization"})
WIRE_APIS: Final = frozenset({"chat", "responses"})
ADAPTERS: Final = frozenset(
    {"anthropic", "google", "openai", "openai-compatible"}
)


class CredentialError(ValueError):
    pass


def validate_headers(pairs: tuple[tuple[str, str], ...]) -> None:
    if not isinstance(pairs, tuple):
        raise CredentialError("headers must be a tuple of pairs")
    if len(pairs) > MAX_HEADERS:
        raise CredentialError(f"at most {MAX_HEADERS} headers are supported")

    seen: set[str] = set()
    for pair in pairs:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise CredentialError("each header must be a name and a value")
        name, value = pair
        if not isinstance(name, str) or not name.strip():
            raise CredentialError("a header needs a name")
        if not isinstance(value, str):
            raise CredentialError(f"header {name!r} must have a text value")
        if len(name) > MAX_HEADER_NAME_CHARACTERS:
            raise CredentialError(f"header {name!r} is longer than allowed")
        if len(value) > MAX_HEADER_VALUE_CHARACTERS:
            raise CredentialError(f"header {name!r} has a value longer than allowed")
        folded = name.casefold()
        if folded in RESERVED_HEADERS:
            raise CredentialError(
                f"header {name!r} is set from the credential and cannot be configured"
            )
        if folded in seen:
            raise CredentialError(f"header {name!r} is set more than once")
        seen.add(folded)


@runtime_checkable
class Credential(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def is_usable(self) -> bool: ...

    def client_options(self) -> dict[str, Any]: ...

    def request_headers(self) -> dict[str, str]: ...

    def endpoint_override(self) -> str | None: ...

    def redacted(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ApiKey:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise CredentialError("an API key cannot be empty")

    @property
    def kind(self) -> str:
        return "api-key"

    @property
    def is_usable(self) -> bool:
        return True

    def client_options(self) -> dict[str, Any]:
        return {"api_key": self.value}

    def request_headers(self) -> dict[str, str]:
        return {}

    def endpoint_override(self) -> str | None:
        return None

    def redacted(self) -> str:
        tail = self.value[-4:] if len(self.value) > 4 else ""
        return f"api key ending {tail}" if tail else "api key"


@dataclass(frozen=True, slots=True)
class Provider:
    name: str = DEFAULT_PROVIDER_NAME
    base_url: str | None = None
    oauth: OAuthClient | None = None
    header_pairs: tuple[tuple[str, str], ...] = ()
    display_name: str = ""
    wire_api: str = "chat"
    adapter: str = "openai-compatible"
    env_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise CredentialError("a provider needs a name")
        if not isinstance(self.display_name, str):
            raise CredentialError("display_name must be text")
        if len(self.display_name) > MAX_DISPLAY_NAME_CHARACTERS:
            raise CredentialError("display_name is longer than allowed")
        if self.base_url is not None and not isinstance(self.base_url, str):
            raise CredentialError("base_url must be text or None")
        if self.oauth is not None and not isinstance(self.oauth, OAuthClient):
            raise CredentialError("oauth must be an OAuthClient or None")
        if self.wire_api not in WIRE_APIS:
            raise CredentialError(f"wire_api must be one of {sorted(WIRE_APIS)}")
        if self.adapter not in ADAPTERS:
            raise CredentialError(f"adapter must be one of {sorted(ADAPTERS)}")
        if not isinstance(self.env_names, tuple):
            raise CredentialError("env_names must be a tuple")
        if len(self.env_names) > MAX_ENV_NAMES:
            raise CredentialError(f"at most {MAX_ENV_NAMES} environment names are supported")
        if any(
            not isinstance(name, str) or not name.strip() or len(name) > 100
            for name in self.env_names
        ):
            raise CredentialError("environment names must be short non-empty strings")
        validate_headers(self.header_pairs)

    @property
    def headers(self) -> dict[str, str]:
        return dict(self.header_pairs)

    @property
    def is_named(self) -> bool:
        return bool(self.display_name) or self.name != DEFAULT_PROVIDER_NAME

    @property
    def label(self) -> str:
        return self.display_name or self.name

    @property
    def models_url(self) -> str:
        root = (self.base_url or "https://api.openai.com/v1").rstrip("/")
        return f"{root}/models"

    def models_url_for(self, credential: Credential | None) -> str:
        if (
            credential is not None
            and credential.kind == "oauth"
            and self.oauth is not None
            and self.oauth.models_url
        ):
            return self.oauth.models_url
        return self.models_url


@dataclass(frozen=True, slots=True)
class ModelInfo:
    identifier: str
    provider: str = DEFAULT_PROVIDER_NAME
    display_name: str = ""
    context_window: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identifier, str) or not self.identifier.strip():
            raise CredentialError("a model needs an identifier")

    @property
    def label(self) -> str:
        return self.display_name or self.identifier

    @property
    def qualified_identifier(self) -> str:
        if self.provider == DEFAULT_PROVIDER_NAME:
            return self.identifier
        return f"{self.provider}/{self.identifier}"

    @property
    def context_label(self) -> str:
        if self.context_window is None:
            return ""
        if self.context_window >= 1_000_000:
            return f"{self.context_window // 1_000_000}M"
        if self.context_window >= 1000:
            return f"{self.context_window // 1000}K"
        return str(self.context_window)

    def matches(self, query: str) -> bool:
        needle = query.strip().casefold()
        if not needle:
            return True
        haystack = f"{self.identifier} {self.display_name} {self.provider}".casefold()
        return all(part in haystack for part in needle.split())


@dataclass(slots=True)
class SessionSettings:
    provider: Provider
    credential: Credential | None
    model: str
    _listeners: list[Any] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.provider, Provider):
            raise CredentialError("provider must be a Provider")
        if not isinstance(self.model, str) or not self.model.strip():
            raise CredentialError("a model identifier is required")

    @property
    def fingerprint(self) -> tuple[str, str | None, str]:
        return (
            self.provider.name,
            self.provider.base_url,
            "" if self.credential is None else self.credential.redacted(),
        )

    def on_connection_change(self, listener) -> None:
        if not callable(listener):
            raise TypeError("listener must be callable")
        self._listeners.append(listener)

    def select_model(self, model: str) -> None:
        if not isinstance(model, str) or not model.strip():
            raise CredentialError("a model identifier is required")
        self.model = model.strip()

    def use(self, provider: Provider, credential: Credential | None) -> None:
        if not isinstance(provider, Provider):
            raise CredentialError("provider must be a Provider")
        changed = self.fingerprint != (
            provider.name,
            provider.base_url,
            "" if credential is None else credential.redacted(),
        )
        self.provider = provider
        self.credential = credential
        if changed:
            self._notify()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()


def settings_from_environment(
    *,
    stored_model: str | None = None,
) -> SessionSettings:
    model = (stored_model or "").strip() or os.getenv("MODEL", "").strip()
    if not model:
        raise CredentialError("MODEL must be set in the environment or a .env file")

    base_url = os.getenv("BASE_URL", "").strip() or None
    if base_url is None:
        from truecoder.providers.openai import openai_provider

        provider = openai_provider()
    else:
        from truecoder.providers.registry import provider_from_url

        provider = provider_from_url(base_url)
    raw_key = next(
        (
            os.getenv(name, "").strip()
            for name in (*provider.env_names, "API_KEY")
            if os.getenv(name, "").strip()
        ),
        "",
    )
    return SessionSettings(
        provider=provider,
        credential=ApiKey(raw_key) if raw_key else None,
        model=model,
    )


def stored_credential(provider: str) -> Credential | None:
    from truecoder.providers.keys import load_keys
    from truecoder.providers.tokens import load_tokens

    token = load_tokens().get(provider)
    if token is not None and token.is_usable:
        return token
    return load_keys().get(provider)


def resolve_settings() -> SessionSettings:
    from truecoder.providers.configuration import selectable_providers
    from truecoder.providers.store import load_selection

    stored = load_selection()
    settings = settings_from_environment(stored_model=stored.model)

    configured = {
        provider.name: provider
        for provider in selectable_providers(settings.provider)
    }
    chosen = configured.get(stored.provider or settings.provider.name)
    if chosen is not None:
        settings.provider = chosen

    remembered = stored_credential(settings.provider.name)
    if remembered is None:
        from truecoder.providers.keys import load_keys

        remembered = load_keys().get(DEFAULT_PROVIDER_NAME)
    if remembered is not None:
        settings.credential = remembered
    return settings
