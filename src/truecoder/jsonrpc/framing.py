from __future__ import annotations

import json
from typing import Any, Final, Protocol, runtime_checkable

JSONRPC_VERSION: Final = "2.0"
MAX_MESSAGE_BYTES: Final = 8 * 1024 * 1024


class ProtocolError(ValueError):
    def __init__(self, message: str, code: str) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@runtime_checkable
class MessageReader(Protocol):
    def feed(self, data: bytes) -> list[dict[str, Any]]: ...


@runtime_checkable
class Framing(Protocol):
    def encode(self, payload: dict[str, Any]) -> bytes: ...

    def reader(self) -> MessageReader: ...


def encode_body(payload: dict[str, Any], *, limit: int = MAX_MESSAGE_BYTES) -> bytes:
    if not isinstance(payload, dict):
        raise ProtocolError(
            "A message payload must be an object.",
            code="invalid_payload",
        )

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > limit:
        raise ProtocolError(
            "The message is too large to send.",
            code="message_too_large",
        )
    return body


def decode_body(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ProtocolError(
            "The message body is not valid JSON.",
            code="invalid_body",
        ) from error

    if not isinstance(payload, dict):
        raise ProtocolError(
            "The message body must be an object.",
            code="invalid_body",
        )
    return payload


def request_message(
    request_id: int | str,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "method": method,
    }
    if params is not None:
        payload["params"] = params
    return payload


def notification_message(
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def response_error(payload: dict[str, Any]) -> str | None:
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    code = error.get("code")
    return f"{message or 'request failed'} (code {code})"
