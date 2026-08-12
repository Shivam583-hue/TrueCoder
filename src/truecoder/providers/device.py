from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Final

from truecoder.providers.oauth import (
    OAuthClient,
    OAuthError,
    OAuthToken,
    parse_token_response,
    post_token,
)

DEVICE_GRANT: Final = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_INTERVAL_SECONDS: Final = 5.0
MIN_INTERVAL_SECONDS: Final = 1.0
MAX_INTERVAL_SECONDS: Final = 60.0
SLOW_DOWN_STEP_SECONDS: Final = 5.0
DEFAULT_EXPIRY_SECONDS: Final = 600.0
MAX_CODE_CHARACTERS: Final = 64
MAX_URL_CHARACTERS: Final = 2048
MAX_DEVICE_RESPONSE_BYTES: Final = 64 * 1024

PENDING: Final = "authorization_pending"
SLOW_DOWN: Final = "slow_down"
EXPIRED: Final = "expired_token"
DENIED: Final = "access_denied"


@dataclass(frozen=True, slots=True)
class DeviceGrant:
    device_code: str
    user_code: str
    verification_url: str
    complete_url: str = ""
    interval: float = DEFAULT_INTERVAL_SECONDS
    expires_in: float = DEFAULT_EXPIRY_SECONDS

    @property
    def best_url(self) -> str:
        return self.complete_url or self.verification_url


def _text(payload: dict[str, Any], *names: str, limit: int) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value[:limit]
    return ""


def _number(payload: dict[str, Any], name: str, fallback: float) -> float:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return fallback
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def parse_device_grant(payload: object) -> DeviceGrant:
    if not isinstance(payload, dict):
        raise OAuthError("the device endpoint did not return an object")

    error = payload.get("error")
    if isinstance(error, str) and error:
        raise OAuthError(f"the provider refused a device code ({error})")

    device_code = _text(payload, "device_code", limit=MAX_URL_CHARACTERS)
    user_code = _text(payload, "user_code", limit=MAX_CODE_CHARACTERS)
    verification = _text(
        payload,
        "verification_uri",
        "verification_url",
        limit=MAX_URL_CHARACTERS,
    )
    if not device_code or not user_code or not verification:
        raise OAuthError("the device endpoint left out a required field")

    interval = min(
        max(
            _number(payload, "interval", DEFAULT_INTERVAL_SECONDS),
            MIN_INTERVAL_SECONDS,
        ),
        MAX_INTERVAL_SECONDS,
    )
    return DeviceGrant(
        device_code=device_code,
        user_code=user_code,
        verification_url=verification,
        complete_url=_text(
            payload,
            "verification_uri_complete",
            "verification_url_complete",
            limit=MAX_URL_CHARACTERS,
        ),
        interval=interval,
        expires_in=_number(payload, "expires_in", DEFAULT_EXPIRY_SECONDS),
    )


def parse_brokered_device_grant(payload: object, client: OAuthClient) -> DeviceGrant:
    if not isinstance(payload, dict):
        raise OAuthError("the device endpoint did not return an object")
    device_code = _text(payload, "device_auth_id", limit=MAX_URL_CHARACTERS)
    user_code = _text(payload, "user_code", limit=MAX_CODE_CHARACTERS)
    if not device_code or not user_code:
        raise OAuthError("the device endpoint left out a required field")
    return DeviceGrant(
        device_code=device_code,
        user_code=user_code,
        verification_url=client.device_verification_url,
        interval=min(
            max(
                _number(payload, "interval", DEFAULT_INTERVAL_SECONDS),
                MIN_INTERVAL_SECONDS,
            ),
            MAX_INTERVAL_SECONDS,
        ),
        expires_in=_number(payload, "expires_in", DEFAULT_EXPIRY_SECONDS),
    )


async def post_device_json(url: str, body: dict[str, str]) -> tuple[int, object]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.post(
                url,
                json=body,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "truecoder/0.1.0",
                },
            )
    except Exception as error:  # noqa: BLE001 - provider transports fail uniformly
        raise OAuthError(f"the device endpoint could not be reached: {error}") from None

    raw = response.content
    if len(raw) > MAX_DEVICE_RESPONSE_BYTES:
        raise OAuthError("the device endpoint returned too much data")
    try:
        return response.status_code, response.json()
    except ValueError:
        raise OAuthError("the device endpoint did not return JSON") from None


async def request_device_grant(client: OAuthClient) -> DeviceGrant:
    if not client.supports_device_code:
        raise OAuthError("this provider does not offer a device code flow")

    if client.uses_brokered_device_code:
        status, payload = await post_device_json(
            client.device_url,
            {"client_id": client.client_id},
        )
        if not 200 <= status < 300:
            raise OAuthError(f"the device endpoint refused the request ({status})")
        return parse_brokered_device_grant(payload, client)

    import httpx

    body = {"client_id": client.client_id}
    if client.scopes:
        body["scope"] = " ".join(client.scopes)

    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.post(
                client.device_url,
                data=body,
                headers={"Accept": "application/json"},
            )
    except Exception as error:  # noqa: BLE001 - any transport failure reads the same
        raise OAuthError(f"the device endpoint could not be reached: {error}") from None

    try:
        payload = response.json()
    except ValueError:
        raise OAuthError("the device endpoint did not return JSON") from None
    return parse_device_grant(payload)


def device_body(client: OAuthClient, *, device_code: str) -> dict[str, str]:
    return {
        "grant_type": DEVICE_GRANT,
        "client_id": client.client_id,
        "device_code": device_code,
    }


async def poll_device_grant(
    client: OAuthClient,
    grant: DeviceGrant,
    *,
    provider: str = "",
    sleep=asyncio.sleep,
    clock=time.monotonic,
) -> OAuthToken:
    if client.uses_brokered_device_code:
        return await _poll_brokered_device_grant(
            client,
            grant,
            provider=provider,
            sleep=sleep,
            clock=clock,
        )

    deadline = clock() + grant.expires_in
    interval = grant.interval

    while True:
        if clock() >= deadline:
            raise OAuthError("the device code expired before it was approved")

        payload = await post_token(
            client, device_body(client, device_code=grant.device_code)
        )
        error = payload.get("error")
        if not isinstance(error, str) or not error:
            return parse_token_response(payload, provider=provider, client=client)

        if error == SLOW_DOWN:
            interval = min(interval + SLOW_DOWN_STEP_SECONDS, MAX_INTERVAL_SECONDS)
        elif error == EXPIRED:
            raise OAuthError("the device code expired before it was approved")
        elif error == DENIED:
            raise OAuthError("the request was declined in the browser")
        elif error != PENDING:
            raise OAuthError(f"the provider refused the device code ({error})")

        await sleep(interval)


async def _poll_brokered_device_grant(
    client: OAuthClient,
    grant: DeviceGrant,
    *,
    provider: str,
    sleep,
    clock,
) -> OAuthToken:
    deadline = clock() + grant.expires_in
    while True:
        if clock() >= deadline:
            raise OAuthError("the device code expired before it was approved")

        status, payload = await post_device_json(
            client.device_token_url,
            {
                "device_auth_id": grant.device_code,
                "user_code": grant.user_code,
            },
        )
        if 200 <= status < 300:
            if not isinstance(payload, dict):
                raise OAuthError("the device token endpoint did not return an object")
            code = _text(payload, "authorization_code", limit=MAX_URL_CHARACTERS)
            verifier = _text(payload, "code_verifier", limit=MAX_URL_CHARACTERS)
            if not code or not verifier:
                raise OAuthError("the device token endpoint left out a required field")
            token_payload = await post_token(
                client,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": client.device_redirect_url,
                    "client_id": client.client_id,
                    "code_verifier": verifier,
                },
            )
            return parse_token_response(
                token_payload,
                provider=provider,
                client=client,
            )
        if status not in {403, 404}:
            raise OAuthError(f"the provider refused the device code ({status})")
        await sleep(grant.interval)
