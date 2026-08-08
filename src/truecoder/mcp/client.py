from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from truecoder.jsonrpc.transport import StdioTransport, TransportError
from truecoder.mcp.models import (
    McpToolDescriptor,
    McpToolResult,
    parse_tool_descriptors,
    parse_tool_result,
)
from truecoder.mcp.protocol import (
    METHOD_CALL_TOOL,
    METHOD_INITIALIZE,
    METHOD_INITIALIZED,
    METHOD_LIST_TOOLS,
    LineFraming,
    call_tool_params,
    initialize_params,
    server_protocol_version,
)

DEFAULT_REQUEST_TIMEOUT: Final = 30.0


class McpClient:
    def __init__(
        self,
        command: list[str] | tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        transport: StdioTransport | None = None,
    ) -> None:
        self._transport = transport or StdioTransport(
            command,
            framing=LineFraming(),
            cwd=cwd,
            env=env,
            request_timeout=request_timeout,
        )
        self._started = False
        self._server_version: str | None = None

    @property
    def running(self) -> bool:
        return self._started and self._transport.running

    @property
    def server_protocol_version(self) -> str | None:
        return self._server_version

    @property
    def stderr_tail(self) -> str:
        return self._transport.stderr_tail

    async def start(self) -> None:
        if self._started:
            return

        await self._transport.start()
        result = await self._transport.request(
            METHOD_INITIALIZE,
            initialize_params(),
        )
        self._server_version = server_protocol_version(result)
        await self._transport.notify(METHOD_INITIALIZED)
        self._started = True

    async def stop(self) -> None:
        self._started = False
        await self._transport.stop()

    async def list_tools(self) -> tuple[McpToolDescriptor, ...]:
        self._require_started()
        result = await self._transport.request(METHOD_LIST_TOOLS, {})
        return parse_tool_descriptors(result)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> McpToolResult:
        self._require_started()
        result = await self._transport.request(
            METHOD_CALL_TOOL,
            call_tool_params(name, arguments),
        )
        return parse_tool_result(result)

    def _require_started(self) -> None:
        if not self._started:
            raise TransportError(
                "The server has not completed its handshake.",
                code="not_initialised",
            )
