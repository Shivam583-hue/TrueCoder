from __future__ import annotations

import json
from typing import Any, Final

JSONRPC_VERSION: Final = "2.0"
HEADER_TERMINATOR: Final = b"\r\n\r\n"
MAX_MESSAGE_BYTES: Final = 8 * 1024 * 1024
MAX_HEADER_BYTES: Final = 8 * 1024


class ProtocolError(ValueError):
    def __init__(self, message: str, code: str) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


def encode_message(payload: dict[str, Any]) -> bytes:
    if not isinstance(payload, dict):
        raise ProtocolError("A message payload must be an object.", code="invalid_payload")

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_MESSAGE_BYTES:
        raise ProtocolError("The message is too large to send.", code="message_too_large")

    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


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


class MessageBuffer:
    def __init__(self, *, max_message_bytes: int = MAX_MESSAGE_BYTES) -> None:
        if max_message_bytes < 1:
            raise ValueError("max_message_bytes must be at least one")

        self._max_message_bytes = max_message_bytes
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self._buffer.extend(data)
        messages: list[dict[str, Any]] = []

        while True:
            message = self._take()
            if message is None:
                return messages
            messages.append(message)

    def _take(self) -> dict[str, Any] | None:
        separator = self._buffer.find(HEADER_TERMINATOR)
        if separator < 0:
            if len(self._buffer) > MAX_HEADER_BYTES:
                raise ProtocolError(
                    "The message header is too large.",
                    code="header_too_large",
                )
            return None

        header_block = bytes(self._buffer[:separator])
        content_length = self._content_length(header_block)
        body_start = separator + len(HEADER_TERMINATOR)
        body_end = body_start + content_length

        if len(self._buffer) < body_end:
            return None

        body = bytes(self._buffer[body_start:body_end])
        del self._buffer[:body_end]

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

    def _content_length(self, header_block: bytes) -> int:
        try:
            headers = header_block.decode("ascii")
        except UnicodeDecodeError as error:
            raise ProtocolError(
                "The message header is not ASCII.",
                code="invalid_header",
            ) from error

        for line in headers.split("\r\n"):
            name, separator, value = line.partition(":")
            if not separator or name.strip().lower() != "content-length":
                continue
            try:
                length = int(value.strip())
            except ValueError as error:
                raise ProtocolError(
                    "Content-Length is not a number.",
                    code="invalid_header",
                ) from error
            if length < 0:
                raise ProtocolError(
                    "Content-Length cannot be negative.",
                    code="invalid_header",
                )
            if length > self._max_message_bytes:
                raise ProtocolError(
                    "The message is larger than the allowed maximum.",
                    code="message_too_large",
                )
            return length

        raise ProtocolError(
            "The message header has no Content-Length.",
            code="invalid_header",
        )
