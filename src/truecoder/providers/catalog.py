from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

from platformdirs import user_cache_path

from truecoder.providers.models import (
    ADAPTERS,
    MAX_MODEL_ID_CHARACTERS,
    MAX_MODEL_NAME_CHARACTERS,
    MAX_RELEASE_DATE_CHARACTERS,
    REASONING_EFFORTS,
    WIRE_APIS,
    Credential,
    ModelInfo,
    Provider,
)
from truecoder.version import package_version

MAX_MODELS: Final = 1000
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
MAX_MODELS_DEV_RESPONSE_BYTES: Final = 16 * 1024 * 1024
CACHE_TTL_SECONDS: Final = 6 * 60 * 60
MODELS_DEV_CACHE_TTL_SECONDS: Final = 5 * 60
REQUEST_TIMEOUT_SECONDS: Final = 20.0
MAX_SLUG_CHARACTERS: Final = 40
MAX_PROVIDERS: Final = 256
EMPTY_CATALOG_REASON: Final = "the provider listed no models"
MODELS_DEV_URL: Final = "https://models.opencode.ai/api.json"

_DEFAULT_PROVIDER_ENDPOINTS: Final = {
    "anthropic": "https://api.anthropic.com/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "cohere": "https://api.cohere.com/compatibility/v1",
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "perplexity": "https://api.perplexity.ai",
    "togetherai": "https://api.together.xyz/v1",
    "venice": "https://api.venice.ai/api/v1",
    "xai": "https://api.x.ai/v1",
}
_UNSUPPORTED_PACKAGES: Final = frozenset(
    {
        "@ai-sdk/amazon-bedrock",
        "@ai-sdk/azure",
        "@ai-sdk/gateway",
        "@ai-sdk/google-vertex",
        "@ai-sdk/google-vertex/anthropic",
        "@ai-sdk/vercel",
        "@aihubmix/ai-sdk-provider",
        "@jerome-benoit/sap-ai-provider-v2",
        "ai-gateway-provider",
        "gitlab-ai-provider",
        "merge-gateway-ai-sdk-provider",
    }
)

_CONTEXT_KEYS: Final = (
    "context",
    "context_length",
    "context_window",
    "max_context_length",
    "max_input_tokens",
)


class CatalogError(RuntimeError):
    pass


def catalog_slug(provider: str) -> str:
    readable = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in provider.strip()
    )
    digest = hashlib.sha256(provider.encode("utf-8")).hexdigest()[:8]
    return f"{readable[:MAX_SLUG_CHARACTERS]}-{digest}"


def catalog_path_for(provider: str) -> Path:
    root = user_cache_path("truecoder", appauthor=False) / "models"
    return root / f"{catalog_slug(provider)}.json"


def models_dev_path() -> Path:
    return user_cache_path("truecoder", appauthor=False) / "models.json"


def _bounded(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    collapsed = " ".join(value.split())
    return collapsed[:limit]


def _api_url(value: object) -> str:
    candidate = _bounded(value, 2048)
    return candidate if urlparse(candidate).scheme == "https" else ""


def _context_window(entry: dict[str, Any]) -> int | None:
    for key in _CONTEXT_KEYS:
        value = entry.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 0 < value <= 100_000_000:
            return value
        if isinstance(value, dict):
            nested = _context_window(value)
            if nested is not None:
                return nested
    top = entry.get("top_provider")
    if isinstance(top, dict):
        return _context_window(top)
    return None


def _reasoning_efforts(entry: dict[str, Any]) -> tuple[str, ...]:
    """Keep only explicit, provider-advertised effort values."""
    values: object = entry.get("supported_reasoning_efforts")
    if not isinstance(values, list):
        values = entry.get("reasoning_efforts")
    if not isinstance(values, list):
        options = entry.get("reasoning_options")
        values = (
            next(
                (
                    option.get("values")
                    for option in options
                    if isinstance(option, dict) and option.get("type") == "effort"
                ),
                (),
            )
            if isinstance(options, list)
            else ()
        )
    if not isinstance(values, (list, tuple)):
        return ()

    advertised: set[str] = set()
    for value in values:
        effort = "none" if value is None else value
        if isinstance(effort, str) and effort in REASONING_EFFORTS:
            advertised.add(effort)
    return tuple(
        effort for effort in REASONING_EFFORTS if effort in advertised
    )


def _models_dev_adapter(value: object) -> str:
    package = value if isinstance(value, str) else ""
    if package == "@ai-sdk/anthropic":
        return "anthropic"
    if package == "@ai-sdk/google":
        return "google"
    if package == "@ai-sdk/openai":
        return "openai"
    if package in _UNSUPPORTED_PACKAGES:
        return "unsupported"
    return "openai-compatible"


def _models_dev_provider(identifier: str, entry: dict[str, Any]) -> Provider:
    from truecoder.providers.openai import openai_provider
    from truecoder.providers.registry import openrouter_provider

    api = _api_url(entry.get("api")) or _DEFAULT_PROVIDER_ENDPOINTS.get(
        identifier
    )
    env = entry.get("env")
    env_names = tuple(
        _bounded(name, 100)
        for name in (env if isinstance(env, list) else [])[:16]
        if _bounded(name, 100)
    )
    label = _bounded(entry.get("name"), 60) or identifier
    package = entry.get("npm")

    if identifier == "openai":
        return openai_provider()
    if identifier == "openrouter":
        provider = openrouter_provider(base_url=api or "https://openrouter.ai/api/v1")
        return Provider(
            name=provider.name,
            base_url=provider.base_url,
            header_pairs=provider.header_pairs,
            display_name=label,
            wire_api=provider.wire_api,
            adapter=provider.adapter,
            env_names=env_names or provider.env_names,
        )
    adapter = _models_dev_adapter(package)
    if api is None:
        adapter = "unsupported"
    return Provider(
        name=identifier,
        display_name=label,
        base_url=api,
        adapter=adapter,
        wire_api="responses" if adapter == "openai" else "chat",
        env_names=env_names,
    )


def _model_transport(
    provider: Provider,
    entry: dict[str, Any],
) -> tuple[str, str, str]:
    override = entry.get("provider")
    if not isinstance(override, dict):
        return "", "", ""
    api = _api_url(override.get("api"))
    package = override.get("npm")
    adapter = _models_dev_adapter(package) if isinstance(package, str) else ""
    if adapter == "openai-compatible" and not (api or provider.base_url):
        adapter = "unsupported"
    wire_api = "responses" if adapter == "openai" else ""
    return api, adapter, wire_api


def parse_models_dev(payload: object) -> tuple[CatalogSlice, ...]:
    if not isinstance(payload, dict):
        return ()

    slices: list[CatalogSlice] = []
    for key, entry in list(payload.items())[:MAX_PROVIDERS]:
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        identifier = _bounded(entry.get("id"), MAX_MODEL_NAME_CHARACTERS)
        identifier = identifier or _bounded(key, MAX_MODEL_NAME_CHARACTERS)
        if not identifier:
            continue
        provider = _models_dev_provider(identifier, entry)
        listed = entry.get("models")
        if not isinstance(listed, dict):
            listed = {}

        models: list[ModelInfo] = []
        for model_key, model_entry in list(listed.items())[:MAX_MODELS]:
            if not isinstance(model_key, str) or not isinstance(model_entry, dict):
                continue
            if model_entry.get("status") in {"alpha", "deprecated"}:
                continue
            model_id = _bounded(
                model_entry.get("id") or model_key,
                MAX_MODEL_ID_CHARACTERS,
            )
            if not model_id:
                continue
            base_url, adapter, wire_api = _model_transport(provider, model_entry)
            models.append(
                ModelInfo(
                    identifier=model_id,
                    provider=provider.name,
                    display_name=_bounded(
                        model_entry.get("name"),
                        MAX_MODEL_NAME_CHARACTERS,
                    ),
                    context_window=_context_window(model_entry),
                    release_date=_bounded(
                        model_entry.get("release_date"),
                        MAX_RELEASE_DATE_CHARACTERS,
                    ),
                    base_url=base_url,
                    adapter=adapter,
                    wire_api=wire_api,
                    reasoning_efforts=_reasoning_efforts(model_entry),
                )
            )
        slices.append(
            CatalogSlice(
                provider,
                tuple(
                    sorted(
                        models,
                        key=lambda model: (model.release_date, model.label),
                        reverse=True,
                    )
                ),
                None if models else EMPTY_CATALOG_REASON,
            )
        )
    return tuple(sorted(slices, key=lambda item: item.provider.label.casefold()))


def decode_models_dev(raw: bytes) -> tuple[CatalogSlice, ...]:
    if len(raw) > MAX_MODELS_DEV_RESPONSE_BYTES:
        raise CatalogError("the Models.dev catalog was larger than allowed")
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        raise CatalogError("the Models.dev catalog was not valid JSON") from None
    slices = parse_models_dev(payload)
    if not slices:
        raise CatalogError("the Models.dev catalog listed no providers")
    return slices


def read_models_dev_cache(
    path: Path,
    *,
    now: float | None = None,
    allow_stale: bool = False,
) -> tuple[CatalogSlice, ...] | None:
    try:
        stat = path.stat()
        current = time.time() if now is None else now
        if not allow_stale and current - stat.st_mtime > MODELS_DEV_CACHE_TTL_SECONDS:
            return None
        raw = path.read_bytes()
        return decode_models_dev(raw)
    except (OSError, CatalogError):
        return None


def write_models_dev_cache(path: Path, raw: bytes) -> None:
    if len(raw) > MAX_MODELS_DEV_RESPONSE_BYTES:
        return
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(raw)
        temporary.replace(path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


async def fetch_models_dev(
    *,
    url: str = MODELS_DEV_URL,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> tuple[tuple[CatalogSlice, ...], bytes]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": f"truecoder/{package_version()}",
                },
            )
    except httpx.HTTPError as error:
        raise CatalogError(f"the Models.dev catalog could not be fetched: {error}") from None
    if response.status_code != 200:
        raise CatalogError(
            f"Models.dev returned {response.status_code} for its catalog"
        )
    raw = response.content
    return decode_models_dev(raw), raw


async def load_models_dev(
    *,
    path: Path | None = None,
    refresh: bool = False,
    url: str = MODELS_DEV_URL,
) -> tuple[CatalogSlice, ...]:
    target = path or models_dev_path()
    if not refresh:
        cached = read_models_dev_cache(target)
        if cached is not None:
            return cached
    try:
        slices, raw = await fetch_models_dev(url=url)
    except CatalogError:
        stale = read_models_dev_cache(target, allow_stale=True)
        if stale is not None:
            return stale
        raise
    write_models_dev_cache(target, raw)
    return slices


def parse_models(payload: object, provider: str) -> tuple[ModelInfo, ...]:
    if not isinstance(payload, dict):
        return ()

    listed = payload.get("data")
    codex_listing = False
    if not isinstance(listed, list):
        listed = payload.get("models")
        codex_listing = isinstance(listed, list)
    if not isinstance(listed, list):
        return ()

    seen: set[str] = set()
    models: list[ModelInfo] = []
    for entry in listed[:MAX_MODELS]:
        if not isinstance(entry, dict):
            continue
        if codex_listing and entry.get("visibility") not in (None, "list"):
            continue
        identifier = _bounded(
            entry.get("slug") if codex_listing else entry.get("id"),
            MAX_MODEL_ID_CHARACTERS,
        )
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        models.append(
            ModelInfo(
                identifier=identifier,
                provider=provider,
                display_name=_bounded(
                    entry.get("display_name") if codex_listing else entry.get("name"),
                    MAX_MODEL_NAME_CHARACTERS,
                ),
                context_window=_context_window(entry),
                reasoning_efforts=_reasoning_efforts(entry),
            )
        )
    return tuple(sorted(models, key=lambda model: model.identifier))


def encode_cache(models: tuple[ModelInfo, ...], *, fetched_at: float) -> str:
    return json.dumps(
        {
            "fetched_at": fetched_at,
            "models": [
                {
                    "id": model.identifier,
                    "provider": model.provider,
                    "name": model.display_name,
                    "context_window": model.context_window,
                    "release_date": model.release_date,
                    "base_url": model.base_url,
                    "adapter": model.adapter,
                    "wire_api": model.wire_api,
                    "reasoning_efforts": list(model.reasoning_efforts),
                }
                for model in models
            ],
        }
    )


def decode_cache(
    raw: str,
    *,
    now: float,
    ttl_seconds: float = CACHE_TTL_SECONDS,
) -> tuple[ModelInfo, ...] | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    fetched_at = payload.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    if now - float(fetched_at) > ttl_seconds:
        return None

    listed = payload.get("models")
    if not isinstance(listed, list):
        return None

    models: list[ModelInfo] = []
    for entry in listed[:MAX_MODELS]:
        if not isinstance(entry, dict):
            continue
        identifier = _bounded(entry.get("id"), MAX_MODEL_ID_CHARACTERS)
        if not identifier:
            continue
        window = entry.get("context_window")
        models.append(
            ModelInfo(
                identifier=identifier,
                provider=_bounded(entry.get("provider"), MAX_MODEL_NAME_CHARACTERS),
                display_name=_bounded(entry.get("name"), MAX_MODEL_NAME_CHARACTERS),
                context_window=window if isinstance(window, int) else None,
                release_date=_bounded(
                    entry.get("release_date"),
                    MAX_RELEASE_DATE_CHARACTERS,
                ),
                base_url=_api_url(entry.get("base_url")),
                adapter=(
                    _bounded(entry.get("adapter"), 40)
                    if _bounded(entry.get("adapter"), 40) in ADAPTERS
                    else ""
                ),
                wire_api=(
                    _bounded(entry.get("wire_api"), 20)
                    if _bounded(entry.get("wire_api"), 20) in WIRE_APIS
                    else ""
                ),
                reasoning_efforts=_reasoning_efforts(entry),
            )
        )
    return tuple(models)


def read_cache(path: Path, *, now: float | None = None) -> tuple[ModelInfo, ...] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return decode_cache(raw, now=time.time() if now is None else now)


def write_cache(
    path: Path,
    models: tuple[ModelInfo, ...],
    *,
    now: float | None = None,
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            encode_cache(models, fetched_at=time.time() if now is None else now),
            encoding="utf-8",
        )
    except OSError:
        return


def bearer_token(credential: Credential | None) -> str:
    if credential is None:
        return ""
    options = credential.client_options()
    secret = options.get("api_key", "") if isinstance(options, dict) else ""
    return secret if isinstance(secret, str) else ""


async def fetch_models(
    provider: Provider,
    credential,
    *,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> tuple[ModelInfo, ...]:
    import httpx

    headers = {"Accept": "application/json", **provider.headers}
    if credential is not None:
        headers.update(credential.request_headers())
    secret = bearer_token(credential)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(
                provider.models_url_for(credential),
                headers=headers,
            )
    except Exception as error:  # noqa: BLE001 - any transport failure is the same
        raise CatalogError(f"the model list could not be fetched: {error}") from None

    if response.status_code != 200:
        raise CatalogError(
            f"the provider returned {response.status_code} for its model list"
        )

    body = response.content[:MAX_RESPONSE_BYTES]
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        raise CatalogError("the model list was not valid JSON") from None

    return parse_models(payload, provider.name)


async def load_models(
    provider: Provider,
    credential,
    *,
    path: Path | None = None,
    refresh: bool = False,
) -> tuple[ModelInfo, ...]:
    target = path or catalog_path_for(provider.name)

    if not refresh:
        cached = read_cache(target)
        if cached:
            return cached

    models = await fetch_models(provider, credential)
    if models:
        write_cache(target, models)
    return models


@dataclass(frozen=True, slots=True)
class CatalogSlice:
    provider: Provider
    models: tuple[ModelInfo, ...] = ()
    reason: str | None = None

    @property
    def supported_models(self) -> tuple[ModelInfo, ...]:
        return tuple(
            model
            for model in self.models
            if model.provider == self.provider.name
            and model.provider_config(self.provider).is_supported
        )

    @property
    def is_supported(self) -> bool:
        return self.provider.is_supported or bool(self.supported_models)


@dataclass(frozen=True, slots=True)
class ProviderCatalog:
    slices: tuple[CatalogSlice, ...]
    credentials: dict[str, Credential | None]
    directory_reason: str | None = None

    @property
    def providers(self) -> tuple[Provider, ...]:
        return tuple(entry.provider for entry in self.slices)


async def _slice_for(
    provider: Provider,
    credential: Credential | None,
    *,
    refresh: bool,
) -> CatalogSlice:
    try:
        models = await load_models(provider, credential, refresh=refresh)
    except CatalogError as error:
        return CatalogSlice(provider, (), str(error))
    if not models:
        return CatalogSlice(provider, (), EMPTY_CATALOG_REASON)
    return CatalogSlice(provider, models)


async def load_catalog(
    providers: tuple[Provider, ...],
    credentials: dict[str, Credential | None],
    *,
    refresh: bool = False,
) -> tuple[CatalogSlice, ...]:
    gathered = await asyncio.gather(
        *(
            _slice_for(provider, credentials.get(provider.name), refresh=refresh)
            for provider in providers
        )
    )
    return tuple(gathered)


def _retain_directory_efforts(
    live: CatalogSlice,
    directory: CatalogSlice,
) -> CatalogSlice:
    known = {model.identifier: model for model in directory.models}
    return CatalogSlice(
        live.provider,
        tuple(
            replace(
                model,
                reasoning_efforts=(
                    model.reasoning_efforts
                    or known.get(model.identifier, model).reasoning_efforts
                ),
            )
            for model in live.models
        ),
        live.reason,
    )


async def discover_catalog(
    settings,
    *,
    refresh: bool = False,
    credential_overrides: dict[str, Credential] | None = None,
) -> ProviderCatalog:
    from truecoder.providers.configuration import selectable_providers
    from truecoder.providers.models import credential_for_provider

    configured = selectable_providers(settings.provider)
    overrides = {provider.name: provider for provider in configured}

    directory_reason: str | None = None
    try:
        directory = await load_models_dev(refresh=refresh)
    except CatalogError as error:
        directory = ()
        directory_reason = str(error)

    resolved: dict[str, CatalogSlice] = {}
    for entry in directory:
        provider = overrides.get(entry.provider.name, entry.provider)
        resolved[provider.name] = CatalogSlice(provider, entry.models, entry.reason)

    overrides_by_name = credential_overrides or {}

    def credential_for(provider: Provider) -> Credential | None:
        override = overrides_by_name.get(provider.name)
        if override is not None and override.is_usable:
            return override
        return credential_for_provider(provider, settings)

    credentials = {
        provider.name: credential_for(provider)
        for provider in (*configured, *(entry.provider for entry in directory))
    }
    dynamic = tuple(
        provider
        for provider in configured
        if provider.name not in resolved
        or (
            credentials.get(provider.name) is not None
            and credentials[provider.name].kind == "oauth"
            and provider.oauth is not None
        )
    )
    if dynamic:
        live = await load_catalog(dynamic, credentials, refresh=refresh)
        for entry in live:
            existing = resolved.get(entry.provider.name)
            if entry.models and existing is not None:
                entry = _retain_directory_efforts(entry, existing)
            if entry.models or existing is None:
                resolved[entry.provider.name] = entry

    slices = tuple(
        sorted(
            resolved.values(),
            key=lambda entry: entry.provider.label.casefold(),
        )
    )
    credentials = {
        entry.provider.name: credential_for(entry.provider)
        for entry in slices
    }
    return ProviderCatalog(slices, credentials, directory_reason)


def merge_models(slices: tuple[CatalogSlice, ...]) -> tuple[ModelInfo, ...]:
    listed = [model for entry in slices for model in entry.supported_models]
    return tuple(
        sorted(listed, key=lambda model: (model.provider, model.identifier))
    )


def catalog_problem(slices: tuple[CatalogSlice, ...]) -> str | None:
    if any(entry.supported_models for entry in slices):
        return None
    reasons = [entry for entry in slices if entry.reason is not None]
    if not reasons:
        return EMPTY_CATALOG_REASON
    if len(reasons) == 1:
        return reasons[0].reason
    return "; ".join(f"{entry.provider.name}: {entry.reason}" for entry in reasons)
