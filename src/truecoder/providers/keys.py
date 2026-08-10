from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Final

from platformdirs import user_config_path

from truecoder.execution.audit.permissions import AuditPermissions
from truecoder.execution.errors import AuditUnavailableError
from truecoder.providers.models import ApiKey, CredentialError

KEYS_VERSION: Final = 1
MAX_KEYS_BYTES: Final = 256 * 1024
MAX_KEY_CHARACTERS: Final = 4096


def default_keys_path() -> Path:
    return user_config_path("truecoder", appauthor=False) / "keys.json"


def encode_keys(keys: dict[str, ApiKey]) -> str:
    return (
        json.dumps(
            {
                "version": KEYS_VERSION,
                "keys": {
                    provider: key.value for provider, key in sorted(keys.items())
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def parse_keys(raw: str) -> dict[str, ApiKey]:
    if not isinstance(raw, str):
        raise CredentialError("stored keys must be text")
    if len(raw.encode("utf-8")) > MAX_KEYS_BYTES:
        raise CredentialError("stored keys are too large")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CredentialError(f"stored keys are not valid JSON: {error}") from None

    if not isinstance(payload, dict):
        raise CredentialError("stored keys must be a JSON object")
    if payload.get("version") != KEYS_VERSION:
        raise CredentialError(f"stored keys version must be {KEYS_VERSION}")

    listed = payload.get("keys")
    if not isinstance(listed, dict):
        return {}

    keys: dict[str, ApiKey] = {}
    for provider, value in listed.items():
        if not isinstance(provider, str) or not provider.strip():
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        if len(value) > MAX_KEY_CHARACTERS:
            raise CredentialError("a stored key is longer than allowed")
        keys[provider] = ApiKey(value)
    return keys


def load_keys(path: Path | None = None) -> dict[str, ApiKey]:
    target = path or default_keys_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return {}
    try:
        return parse_keys(raw)
    except CredentialError:
        return {}


def save_keys(
    keys: dict[str, ApiKey],
    path: Path | None = None,
    *,
    permissions: AuditPermissions | None = None,
) -> bool:
    target = path or default_keys_path()
    guard = permissions or AuditPermissions()

    try:
        guard.prepare(target)
        handle, temporary = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=".keys-",
            suffix=".json",
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(encode_keys(keys))
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except (OSError, AuditUnavailableError):
        return False

    guard.secure_sidecars(target)
    return True


def store_key(
    provider: str,
    key: ApiKey,
    path: Path | None = None,
    *,
    permissions: AuditPermissions | None = None,
) -> bool:
    if not isinstance(provider, str) or not provider.strip():
        raise CredentialError("a key must name the provider it belongs to")
    if not isinstance(key, ApiKey):
        raise CredentialError("key must be an ApiKey")
    if len(key.value) > MAX_KEY_CHARACTERS:
        raise CredentialError("the key is longer than allowed")

    target = path or default_keys_path()
    keys = load_keys(target)
    keys[provider.strip()] = key
    return save_keys(keys, target, permissions=permissions)


def forget_key(
    provider: str,
    path: Path | None = None,
    *,
    permissions: AuditPermissions | None = None,
) -> bool:
    target = path or default_keys_path()
    keys = load_keys(target)
    if provider not in keys:
        return False
    del keys[provider]
    return save_keys(keys, target, permissions=permissions)
