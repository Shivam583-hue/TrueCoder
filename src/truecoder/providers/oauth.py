from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qs, urlencode, urlparse

VERIFIER_BYTES: Final = 64
STATE_BYTES: Final = 32
MAX_CLAIM_CHARACTERS: Final = 8 * 1024
MAX_ACCOUNT_CHARACTERS: Final = 200
REFRESH_MARGIN_SECONDS: Final = 60.0
CALLBACK_TIMEOUT_SECONDS: Final = 300.0
MAX_CALLBACK_BYTES: Final = 16 * 1024
CALLBACK_HOST: Final = "127.0.0.1"

SUCCESS_BODY: Final = (
    "<!doctype html><title>TrueCoder</title>"
    "<p>Authorised. You can close this tab and return to the terminal.</p>"
)
FAILURE_BODY: Final = (
    "<!doctype html><title>TrueCoder</title>"
    "<p>Authorisation failed. Return to the terminal for the reason.</p>"
)


class OAuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OAuthClient:
    client_id: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...] = ()
    account_claim: str = ""
    account_header: str = ""
    api_base_url: str = ""
    extra_parameters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in ("client_id", "authorize_url", "token_url"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise OAuthError(f"{name} is required for an OAuth provider")
        for name in ("authorize_url", "token_url"):
            scheme = urlparse(getattr(self, name)).scheme
            if scheme != "https":
                raise OAuthError(f"{name} must be an https URL")
        if self.api_base_url and urlparse(self.api_base_url).scheme != "https":
            raise OAuthError("api_base_url must be an https URL")
        if bool(self.account_claim) != bool(self.account_header):
            raise OAuthError(
                "account_claim and account_header are only useful together"
            )
        if self.account_header and self.account_header.casefold() == "authorization":
            raise OAuthError("account_header cannot be the authorisation header")

    @property
    def carries_account(self) -> bool:
        return bool(self.account_claim and self.account_header)


@dataclass(frozen=True, slots=True)
class PkcePair:
    verifier: str
    challenge: str

    @property
    def method(self) -> str:
        return "S256"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_pkce() -> PkcePair:
    verifier = _b64(secrets.token_bytes(VERIFIER_BYTES))
    challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
    return PkcePair(verifier=verifier, challenge=challenge)


def generate_state() -> str:
    return _b64(secrets.token_bytes(STATE_BYTES))


def authorization_url(
    client: OAuthClient,
    *,
    redirect_uri: str,
    pkce: PkcePair,
    state: str,
) -> str:
    parameters = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.method,
    }
    if client.scopes:
        parameters["scope"] = " ".join(client.scopes)
    for key, value in client.extra_parameters:
        parameters.setdefault(key, value)
    separator = "&" if "?" in client.authorize_url else "?"
    return f"{client.authorize_url}{separator}{urlencode(parameters)}"


@dataclass(frozen=True, slots=True)
class CallbackResult:
    code: str | None = None
    error: str | None = None


def read_callback(target: str, expected_state: str) -> CallbackResult:
    query = parse_qs(urlparse(target).query)

    received_state = query.get("state", [""])[0]
    if not secrets.compare_digest(received_state, expected_state):
        return CallbackResult(error="the authorisation state did not match")

    error = query.get("error", [""])[0]
    if error:
        description = query.get("error_description", [""])[0]
        return CallbackResult(error=f"{error}: {description}" if description else error)

    code = query.get("code", [""])[0]
    if not code:
        return CallbackResult(error="the provider returned no authorisation code")
    return CallbackResult(code=code)


@dataclass(frozen=True, slots=True)
class OAuthToken:
    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None
    provider: str = ""
    metadata: tuple[tuple[str, str], ...] = ()
    endpoint: str = ""
    _clock: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.access_token, str) or not self.access_token.strip():
            raise OAuthError("an access token cannot be empty")

    @property
    def kind(self) -> str:
        return "oauth"

    def _now(self) -> float:
        return time.time() if self._clock is None else float(self._clock())

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return self._now() >= self.expires_at

    @property
    def needs_refresh(self) -> bool:
        if self.expires_at is None:
            return False
        return self._now() >= self.expires_at - REFRESH_MARGIN_SECONDS

    @property
    def is_usable(self) -> bool:
        return not self.is_expired or self.refresh_token is not None

    def client_options(self) -> dict[str, Any]:
        return {"api_key": self.access_token}

    def request_headers(self) -> dict[str, str]:
        return dict(self.metadata)

    def endpoint_override(self) -> str | None:
        return self.endpoint or None

    def redacted(self) -> str:
        tail = self.access_token[-4:] if len(self.access_token) > 4 else ""
        suffix = f" ending {tail}" if tail else ""
        return f"oauth token{suffix}"


def token_claims(token: object) -> dict[str, Any]:
    if not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) != 3 or len(parts[1]) > MAX_CLAIM_CHARACTERS:
        return {}

    body = parts[1]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, binascii.Error):
        return {}

    try:
        claims = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return {}
    return claims if isinstance(claims, dict) else {}


def find_claim(claims: dict[str, Any], name: str) -> str:
    if not name:
        return ""
    value = claims.get(name)
    if isinstance(value, str) and value.strip():
        return value[:MAX_ACCOUNT_CHARACTERS]

    for nested in claims.values():
        if not isinstance(nested, dict):
            continue
        inner = nested.get(name)
        if isinstance(inner, str) and inner.strip():
            return inner[:MAX_ACCOUNT_CHARACTERS]
    return ""


def account_metadata(
    payload: dict[str, Any],
    client: OAuthClient | None,
) -> tuple[tuple[str, str], ...]:
    if client is None or not client.carries_account:
        return ()
    for field_name in ("id_token", "access_token"):
        account = find_claim(token_claims(payload.get(field_name)), client.account_claim)
        if account:
            return ((client.account_header, account),)
    return ()


def parse_token_response(
    payload: object,
    *,
    provider: str = "",
    now: float | None = None,
    client: OAuthClient | None = None,
) -> OAuthToken:
    if not isinstance(payload, dict):
        raise OAuthError("the token response was not a JSON object")

    error = payload.get("error")
    if isinstance(error, str) and error:
        description = payload.get("error_description")
        detail = f"{error}: {description}" if isinstance(description, str) else error
        raise OAuthError(f"the provider refused the token request ({detail})")

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise OAuthError("the token response carried no access token")

    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    moment = time.time() if now is None else now
    expires_at: float | None = None
    usable_expiry = (
        isinstance(expires_in, (int, float))
        and not isinstance(expires_in, bool)
        and expires_in > 0
    )
    if usable_expiry:
        expires_at = moment + float(expires_in)

    return OAuthToken(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        expires_at=expires_at,
        provider=provider,
        metadata=account_metadata(payload, client),
        endpoint=client.api_base_url if client is not None else "",
    )


def exchange_body(
    client: OAuthClient,
    *,
    code: str,
    redirect_uri: str,
    verifier: str,
) -> dict[str, str]:
    return {
        "grant_type": "authorization_code",
        "client_id": client.client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }


def refresh_body(client: OAuthClient, *, refresh_token: str) -> dict[str, str]:
    return {
        "grant_type": "refresh_token",
        "client_id": client.client_id,
        "refresh_token": refresh_token,
    }


async def post_token(client: OAuthClient, body: dict[str, str]) -> dict[str, Any]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.post(
                client.token_url,
                data=body,
                headers={"Accept": "application/json"},
            )
    except Exception as error:  # noqa: BLE001 - any transport failure reads the same
        raise OAuthError(f"the token endpoint could not be reached: {error}") from None

    try:
        payload = response.json()
    except ValueError:
        raise OAuthError("the token endpoint did not return JSON") from None

    if not isinstance(payload, dict):
        raise OAuthError("the token endpoint did not return an object")
    return payload


async def refresh_token(client: OAuthClient, token: OAuthToken) -> OAuthToken:
    if token.refresh_token is None:
        raise OAuthError("this token cannot be refreshed")

    payload = await post_token(
        client, refresh_body(client, refresh_token=token.refresh_token)
    )
    refreshed = parse_token_response(payload, provider=token.provider, client=client)
    return OAuthToken(
        access_token=refreshed.access_token,
        refresh_token=refreshed.refresh_token or token.refresh_token,
        expires_at=refreshed.expires_at,
        provider=token.provider,
        metadata=refreshed.metadata or token.metadata,
        endpoint=refreshed.endpoint or token.endpoint,
    )
