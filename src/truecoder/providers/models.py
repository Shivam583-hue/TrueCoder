from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from truecoder.providers.oauth import OAuthClient

MAX_MODEL_ID_CHARACTERS: Final = 200
MAX_MODEL_NAME_CHARACTERS: Final = 120
MAX_RELEASE_DATE_CHARACTERS: Final = 20
DEFAULT_PROVIDER_NAME: Final = "default"
MAX_DISPLAY_NAME_CHARACTERS: Final = 60
MAX_HEADERS: Final = 16
MAX_ENV_NAMES: Final = 16
MAX_HEADER_NAME_CHARACTERS: Final = 64
MAX_HEADER_VALUE_CHARACTERS: Final = 1024
RESERVED_HEADERS: Final = frozenset({"authorization"})
WIRE_APIS: Final = frozenset({"chat", "responses"})
ADAPTERS: Final = frozenset(
    {"anthropic", "google", "openai", "openai-compatible", "unsupported"}
)
REASONING_EFFORTS: Final = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
DEFAULT_REASONING_EFFORT: Final = "xhigh"
_REASONING_EFFORT_SET: Final = frozenset(REASONING_EFFORTS)


class CredentialError(ValueError):
    pass


def validate_reasoning_effort(value: str) -> str:
    if not isinstance(value, str):
        raise CredentialError("reasoning effort must be text")
    effort = value.strip().casefold()
    if effort not in _REASONING_EFFORT_SET:
        choices = ", ".join(REASONING_EFFORTS)
        raise CredentialError(f"reasoning effort must be one of: {choices}")
    return effort


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
    def is_supported(self) -> bool:
        return self.adapter != "unsupported"

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
    release_date: str = ""
    base_url: str = ""
    adapter: str = ""
    wire_api: str = ""
    reasoning_efforts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.identifier, str) or not self.identifier.strip():
            raise CredentialError("a model needs an identifier")
        if not isinstance(self.release_date, str):
            raise CredentialError("release_date must be text")
        if len(self.release_date) > MAX_RELEASE_DATE_CHARACTERS:
            raise CredentialError("release_date is longer than allowed")
        if not isinstance(self.base_url, str):
            raise CredentialError("model base_url must be text")
        if self.adapter and self.adapter not in ADAPTERS:
            raise CredentialError(f"model adapter must be one of {sorted(ADAPTERS)}")
        if self.wire_api and self.wire_api not in WIRE_APIS:
            raise CredentialError(
                f"model wire_api must be one of {sorted(WIRE_APIS)}"
            )
        if not isinstance(self.reasoning_efforts, tuple):
            raise CredentialError("reasoning_efforts must be a tuple")
        if len(set(self.reasoning_efforts)) != len(self.reasoning_efforts):
            raise CredentialError("reasoning_efforts cannot repeat a value")
        for effort in self.reasoning_efforts:
            validate_reasoning_effort(effort)

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

    def provider_config(self, provider: Provider) -> Provider:
        if provider.name != self.provider:
            raise CredentialError("a model can only configure its own provider")
        return Provider(
            name=provider.name,
            base_url=self.base_url or provider.base_url,
            oauth=provider.oauth,
            header_pairs=provider.header_pairs,
            display_name=provider.display_name,
            wire_api=self.wire_api or provider.wire_api,
            adapter=self.adapter or provider.adapter,
            env_names=provider.env_names,
        )


@dataclass(slots=True)
class SessionSettings:
    provider: Provider
    credential: Credential | None
    model: str
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    reasoning_efforts: tuple[str, ...] = ()
    _listeners: list[Any] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.provider, Provider):
            raise CredentialError("provider must be a Provider")
        if not isinstance(self.model, str):
            raise CredentialError("model must be text")
        self.model = self.model.strip()
        self.reasoning_effort = validate_reasoning_effort(self.reasoning_effort)
        if not isinstance(self.reasoning_efforts, tuple):
            raise CredentialError("reasoning_efforts must be a tuple")
        for effort in self.reasoning_efforts:
            validate_reasoning_effort(effort)
        self._fit_reasoning_effort()

    @property
    def has_model(self) -> bool:
        return bool(self.model)

    @property
    def available_reasoning_efforts(self) -> tuple[str, ...]:
        if not self.reasoning_efforts:
            return ()
        if self.provider.wire_api == "responses":
            return self.reasoning_efforts
        if self.provider.adapter in {"openai", "openai-compatible"}:
            return self.reasoning_efforts
        return ()

    @property
    def uses_reasoning_effort(self) -> bool:
        return self.reasoning_effort in self.available_reasoning_efforts

    @property
    def fingerprint(self) -> tuple[str, str | None, str, str, str]:
        return (
            self.provider.name,
            self.provider.base_url,
            self.provider.adapter,
            self.provider.wire_api,
            "" if self.credential is None else self.credential.redacted(),
        )

    def on_connection_change(self, listener) -> None:
        if not callable(listener):
            raise TypeError("listener must be callable")
        self._listeners.append(listener)

    def select_model(
        self,
        model: str,
        *,
        reasoning_efforts: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise CredentialError("a model identifier is required")
        self.model = model.strip()
        if not isinstance(reasoning_efforts, tuple):
            raise CredentialError("reasoning_efforts must be a tuple")
        for effort in reasoning_efforts:
            validate_reasoning_effort(effort)
        self.reasoning_efforts = reasoning_efforts
        self._fit_reasoning_effort()

    def select_reasoning_effort(self, effort: str) -> None:
        selected = validate_reasoning_effort(effort)
        available = self.available_reasoning_efforts
        if selected not in available:
            choices = ", ".join(available) or "none"
            raise CredentialError(
                f"{self.model} supports these reasoning efforts: {choices}"
            )
        self.reasoning_effort = selected

    def _fit_reasoning_effort(self) -> None:
        if not self.reasoning_efforts:
            return
        if self.reasoning_effort not in self.reasoning_efforts:
            self.reasoning_effort = self.reasoning_efforts[-1]

    def use(self, provider: Provider, credential: Credential | None) -> None:
        if not isinstance(provider, Provider):
            raise CredentialError("provider must be a Provider")
        changed = self.fingerprint != (
            provider.name,
            provider.base_url,
            provider.adapter,
            provider.wire_api,
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
    stored_reasoning_effort: str | None = None,
) -> SessionSettings:
    model = (stored_model or "").strip() or os.getenv("MODEL", "").strip()

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
        reasoning_effort=(stored_reasoning_effort or DEFAULT_REASONING_EFFORT),
    )


def stored_credential(provider: str) -> Credential | None:
    from truecoder.providers.keys import load_keys
    from truecoder.providers.tokens import load_tokens

    token = load_tokens().get(provider)
    if token is not None and token.is_usable:
        return token
    return load_keys().get(provider)


def environment_credential(provider: Provider) -> Credential | None:
    raw = next(
        (
            os.getenv(name, "").strip()
            for name in provider.env_names
            if os.getenv(name, "").strip()
        ),
        "",
    )
    return ApiKey(raw) if raw else None


def credential_for_provider(
    provider: Provider,
    active: SessionSettings | None = None,
) -> Credential | None:
    if (
        active is not None
        and active.provider.name == provider.name
        and active.credential is not None
        and active.credential.is_usable
    ):
        return active.credential
    return stored_credential(provider.name) or environment_credential(provider)


def resolve_settings() -> SessionSettings:
    from truecoder.providers.configuration import selectable_providers
    from truecoder.providers.store import load_selection

    stored = load_selection()
    settings = settings_from_environment(
        stored_model=stored.model,
        stored_reasoning_effort=stored.reasoning_effort,
    )

    configured = {
        provider.name: provider
        for provider in selectable_providers(settings.provider)
    }
    chosen = configured.get(stored.provider or settings.provider.name)
    if chosen is None and stored.provider:
        from truecoder.providers.catalog import models_dev_path, read_models_dev_cache

        cached = read_models_dev_cache(models_dev_path(), allow_stale=True) or ()
        chosen = next(
            (
                entry.provider
                for entry in cached
                if entry.provider.name == stored.provider
            ),
            None,
        )
    if chosen is not None:
        from truecoder.providers.catalog import models_dev_path, read_models_dev_cache

        cached = read_models_dev_cache(models_dev_path(), allow_stale=True) or ()
        selected = next(
            (
                model
                for entry in cached
                if entry.provider.name == chosen.name
                for model in entry.models
                if model.identifier == settings.model
            ),
            None,
        )
        if selected is not None:
            chosen = selected.provider_config(chosen)
            settings.select_model(
                settings.model,
                reasoning_efforts=selected.reasoning_efforts,
            )
        settings.provider = chosen

    remembered = credential_for_provider(settings.provider)
    if remembered is None:
        from truecoder.providers.keys import load_keys

        remembered = load_keys().get(DEFAULT_PROVIDER_NAME)
    if remembered is not None:
        settings.credential = remembered
    return settings
