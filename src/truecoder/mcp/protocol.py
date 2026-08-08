from __future__ import annotations

from typing import Any, Final

from truecoder.jsonrpc.framing import (
    MAX_MESSAGE_BYTES,
    ProtocolError,
    decode_body,
    encode_body,
)

PROTOCOL_VERSION: Final = "2025-06-18"
CLIENT_NAME: Final = "truecoder"

METHOD_INITIALIZE: Final = "initialize"
METHOD_INITIALIZED: Final = "notifications/initialized"
METHOD_LIST_TOOLS: Final = "tools/list"
METHOD_CALL_TOOL: Final = "tools/call"


class LineFraming:
    def __init__(self, *, max_message_bytes: int = MAX_MESSAGE_BYTES) -> None:
        if max_message_bytes < 1:
            raise ValueError("max_message_bytes must be at least one")
        self._max_message_bytes = max_message_bytes

    def encode(self, payload: dict[str, Any]) -> bytes:
        return encode_body(payload, limit=self._max_message_bytes) + b"\n"

    def reader(self) -> LineBuffer:
        return LineBuffer(max_message_bytes=self._max_message_bytes)


class LineBuffer:
    def __init__(self, *, max_message_bytes: int = MAX_MESSAGE_BYTES) -> None:
        if max_message_bytes < 1:
            raise ValueError("max_message_bytes must be at least one")
        self._max_message_bytes = max_message_bytes
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self._buffer.extend(data)
        messages: list[dict[str, Any]] = []

        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                if len(self._buffer) > self._max_message_bytes:
                    raise ProtocolError(
                        "The message is too large to receive.",
                        code="message_too_large",
                    )
                return messages

            line = bytes(self._buffer[:index])
            del self._buffer[: index + 1]
            stripped = line.strip()
            if not stripped:
                continue
            if len(stripped) > self._max_message_bytes:
                raise ProtocolError(
                    "The message is too large to receive.",
                    code="message_too_large",
                )
            messages.append(decode_body(stripped))


def initialize_params(capabilities: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": capabilities if capabilities is not None else {},
        "clientInfo": {"name": CLIENT_NAME, "version": "0.1.0"},
    }


def call_tool_params(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a tool name is required")
    if not isinstance(arguments, dict):
        raise TypeError("arguments must be a JSON object")
    return {"name": name, "arguments": arguments}


def server_protocol_version(result: dict[str, Any]) -> str:
    version = result.get("protocolVersion")
    if not isinstance(version, str) or not version.strip():
        raise ProtocolError(
            "The server did not state a protocol version.",
            code="missing_protocol_version",
        )
    return version
