from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from platformdirs import user_cache_path

from truecoder.providers.models import (
    MAX_MODEL_ID_CHARACTERS,
    MAX_MODEL_NAME_CHARACTERS,
    MAX_RELEASE_DATE_CHARACTERS,
    Credential,
    ModelInfo,
    Provider,
)

MAX_MODELS: Final = 1000
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
MAX_MODELS_DEV_RESPONSE_BYTES: Final = 16 * 1024 * 1024
CACHE_TTL_SECONDS: Final = 6 * 60 * 60
MODELS_DEV_CACHE_TTL_SECONDS: Final = 5 * 60
REQUEST_TIMEOUT_SECONDS: Final = 20.0
MAX_SLUG_CHARACTERS: Final = 40
MAX_PROVIDERS: Final = 256
EMPTY_CATALOG_REASON: Final = "the provider listed no models"
MODELS_DEV_URL: Final = "https://models.dev/api.json"

_CONTEXT_KEYS: Final = (
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


def _models_dev_adapter(value: object) -> str:
    package = value if isinstance(value, str) else ""
    if package == "@ai-sdk/anthropic":
        return "anthropic"
    if package == "@ai-sdk/google":
        return "google"
    if package == "@ai-sdk/openai":
        return "openai"
    return "openai-compatible"


def _models_dev_provider(identifier: str, entry: dict[str, Any]) -> Provider:
    from truecoder.providers.openai import openai_provider
    from truecoder.providers.registry import openrouter_provider

    api = _bounded(entry.get("api"), 2048) or None
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
    return Provider(
        name=identifier,
        display_name=label,
        base_url=api,
        adapter=_models_dev_adapter(package),
        env_names=env_names,
    )


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
            if model_entry.get("status") == "deprecated":
                continue
            model_id = _bounded(
                model_entry.get("id") or model_key,
                MAX_MODEL_ID_CHARACTERS,
            )
            if not model_id:
                continue
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
                    "User-Agent": "truecoder/0.1.0",
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


def merge_models(slices: tuple[CatalogSlice, ...]) -> tuple[ModelInfo, ...]:
    listed = [model for entry in slices for model in entry.models]
    return tuple(
        sorted(listed, key=lambda model: (model.provider, model.identifier))
    )


def catalog_problem(slices: tuple[CatalogSlice, ...]) -> str | None:
    if any(entry.models for entry in slices):
        return None
    reasons = [entry for entry in slices if entry.reason is not None]
    if not reasons:
        return EMPTY_CATALOG_REASON
    if len(reasons) == 1:
        return reasons[0].reason
    return "; ".join(f"{entry.provider.name}: {entry.reason}" for entry in reasons)
