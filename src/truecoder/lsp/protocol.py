from __future__ import annotations

from typing import Any, Final

from truecoder.jsonrpc.framing import (
    JSONRPC_VERSION,
    MAX_MESSAGE_BYTES,
    ProtocolError,
    decode_body,
    encode_body,
    notification_message,
    request_message,
    response_error,
)

HEADER_TERMINATOR: Final = b"\r\n\r\n"
MAX_HEADER_BYTES: Final = 8 * 1024

__all__ = [
    "HEADER_TERMINATOR",
    "JSONRPC_VERSION",
    "MAX_HEADER_BYTES",
    "MAX_MESSAGE_BYTES",
    "HeaderFraming",
    "MessageBuffer",
    "ProtocolError",
    "encode_message",
    "notification_message",
    "request_message",
    "response_error",
]


def encode_message(payload: dict[str, Any]) -> bytes:
    body = encode_body(payload)
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


class HeaderFraming:
    def encode(self, payload: dict[str, Any]) -> bytes:
        return encode_message(payload)

    def reader(self) -> MessageBuffer:
        return MessageBuffer()


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

        return decode_body(body)

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
