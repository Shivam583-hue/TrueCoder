from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Final

MAX_MODEL_ID_CHARACTERS: Final = 200
MAX_MODEL_NAME_CHARACTERS: Final = 120
DEFAULT_PROVIDER_NAME: Final = "default"


class CredentialError(ValueError):
    pass


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

    def redacted(self) -> str:
        tail = self.value[-4:] if len(self.value) > 4 else ""
        return f"api key ending {tail}" if tail else "api key"


Credential = ApiKey


@dataclass(frozen=True, slots=True)
class Provider:
    name: str = DEFAULT_PROVIDER_NAME
    base_url: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise CredentialError("a provider needs a name")
        if self.base_url is not None and not isinstance(self.base_url, str):
            raise CredentialError("base_url must be text or None")

    @property
    def models_url(self) -> str:
        root = (self.base_url or "https://api.openai.com/v1").rstrip("/")
        return f"{root}/models"


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
    def context_label(self) -> str:
        if self.context_window is None:
            return ""
        if self.context_window >= 1000:
            return f"{self.context_window // 1000}K"
        return str(self.context_window)

    def matches(self, query: str) -> bool:
        needle = query.strip().casefold()
        if not needle:
            return True
        haystack = f"{self.identifier} {self.display_name}".casefold()
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


def settings_from_environment() -> SessionSettings:
    model = os.getenv("MODEL", "").strip()
    if not model:
        raise CredentialError("MODEL must be set in the environment or a .env file")

    raw_key = os.getenv("API_KEY", "").strip()
    base_url = os.getenv("BASE_URL", "").strip() or None
    return SessionSettings(
        provider=Provider(name=DEFAULT_PROVIDER_NAME, base_url=base_url),
        credential=ApiKey(raw_key) if raw_key else None,
        model=model,
    )
