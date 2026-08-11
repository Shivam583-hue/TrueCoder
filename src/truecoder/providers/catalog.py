from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from platformdirs import user_cache_path

from truecoder.providers.models import (
    MAX_MODEL_ID_CHARACTERS,
    MAX_MODEL_NAME_CHARACTERS,
    Credential,
    ModelInfo,
    Provider,
)

MAX_MODELS: Final = 1000
MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
CACHE_TTL_SECONDS: Final = 6 * 60 * 60
REQUEST_TIMEOUT_SECONDS: Final = 20.0
MAX_SLUG_CHARACTERS: Final = 40
EMPTY_CATALOG_REASON: Final = "the provider listed no models"

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


def parse_models(payload: object, provider: str) -> tuple[ModelInfo, ...]:
    if not isinstance(payload, dict):
        return ()

    listed = payload.get("data")
    if not isinstance(listed, list):
        return ()

    seen: set[str] = set()
    models: list[ModelInfo] = []
    for entry in listed[:MAX_MODELS]:
        if not isinstance(entry, dict):
            continue
        identifier = _bounded(entry.get("id"), MAX_MODEL_ID_CHARACTERS)
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        models.append(
            ModelInfo(
                identifier=identifier,
                provider=provider,
                display_name=_bounded(
                    entry.get("name"),
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

    headers = {"Accept": "application/json"}
    secret = bearer_token(credential)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(provider.models_url, headers=headers)
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
