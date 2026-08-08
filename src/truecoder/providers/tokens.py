from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Final

from platformdirs import user_config_path

from truecoder.execution.audit.permissions import AuditPermissions
from truecoder.execution.errors import AuditUnavailableError
from truecoder.providers.oauth import OAuthError, OAuthToken

TOKENS_VERSION: Final = 1
MAX_TOKENS_BYTES: Final = 256 * 1024
MAX_TOKEN_CHARACTERS: Final = 8192


def default_tokens_path() -> Path:
    return user_config_path("truecoder", appauthor=False) / "tokens.json"


def _token_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    if len(value) > MAX_TOKEN_CHARACTERS:
        raise OAuthError(f"the stored {field} is longer than allowed")
    return value


def encode_tokens(tokens: dict[str, OAuthToken]) -> str:
    return (
        json.dumps(
            {
                "version": TOKENS_VERSION,
                "tokens": {
                    provider: {
                        "access_token": token.access_token,
                        "refresh_token": token.refresh_token,
                        "expires_at": token.expires_at,
                    }
                    for provider, token in sorted(tokens.items())
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def parse_tokens(raw: str) -> dict[str, OAuthToken]:
    if not isinstance(raw, str):
        raise OAuthError("stored tokens must be text")
    if len(raw.encode("utf-8")) > MAX_TOKENS_BYTES:
        raise OAuthError("stored tokens are too large")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise OAuthError(f"stored tokens are not valid JSON: {error}") from None

    if not isinstance(payload, dict):
        raise OAuthError("stored tokens must be a JSON object")
    if payload.get("version") != TOKENS_VERSION:
        raise OAuthError(f"stored tokens version must be {TOKENS_VERSION}")

    listed = payload.get("tokens")
    if not isinstance(listed, dict):
        return {}

    tokens: dict[str, OAuthToken] = {}
    for provider, entry in listed.items():
        if not isinstance(provider, str) or not provider.strip():
            continue
        if not isinstance(entry, dict):
            continue
        access = _token_text(entry.get("access_token"), "access token")
        if access is None:
            continue
        expires_at = entry.get("expires_at")
        tokens[provider] = OAuthToken(
            access_token=access,
            refresh_token=_token_text(entry.get("refresh_token"), "refresh token"),
            expires_at=(
                float(expires_at)
                if isinstance(expires_at, (int, float))
                and not isinstance(expires_at, bool)
                else None
            ),
            provider=provider,
        )
    return tokens


def load_tokens(path: Path | None = None) -> dict[str, OAuthToken]:
    target = path or default_tokens_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return {}
    try:
        return parse_tokens(raw)
    except OAuthError:
        return {}


def save_tokens(
    tokens: dict[str, OAuthToken],
    path: Path | None = None,
    *,
    permissions: AuditPermissions | None = None,
) -> bool:
    target = path or default_tokens_path()
    guard = permissions or AuditPermissions()

    try:
        guard.prepare(target)
        handle, temporary = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=".tokens-",
            suffix=".json",
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(encode_tokens(tokens))
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except (OSError, AuditUnavailableError):
        return False

    guard.secure_sidecars(target)
    return True


def store_token(
    token: OAuthToken,
    path: Path | None = None,
    *,
    permissions: AuditPermissions | None = None,
) -> bool:
    if not token.provider:
        raise OAuthError("a token must name the provider it belongs to")
    tokens = load_tokens(path)
    tokens[token.provider] = token
    return save_tokens(tokens, path, permissions=permissions)


def forget_token(provider: str, path: Path | None = None) -> bool:
    tokens = load_tokens(path)
    if provider not in tokens:
        return False
    del tokens[provider]
    save_tokens(tokens, path)
    return True
