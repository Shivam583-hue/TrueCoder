"""A minimal MCP server used to exercise the client against a real process."""

from __future__ import annotations

import json
import os
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Return the text it was given.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "left": {"type": "number"},
                "right": {"type": "number"},
            },
            "required": ["left", "right"],
        },
    },
]


def _write(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _result(request_id, result: dict) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id, message: str) -> None:
    _write(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32000, "message": message},
        }
    )


def _call(request_id, params: dict, mode: str) -> None:
    name = params.get("name")
    arguments = params.get("arguments") or {}

    if mode == "tool_error":
        _result(
            request_id,
            {
                "content": [{"type": "text", "text": "the tool refused"}],
                "isError": True,
            },
        )
        return

    if name == "echo":
        _result(
            request_id,
            {"content": [{"type": "text", "text": str(arguments.get("text", ""))}]},
        )
        return

    if name == "add":
        total = float(arguments.get("left", 0)) + float(arguments.get("right", 0))
        _result(request_id, {"content": [{"type": "text", "text": str(total)}]})
        return

    if name == "huge":
        _result(
            request_id,
            {"content": [{"type": "text", "text": "y" * 200000}]},
        )
        return

    _error(request_id, f"unknown tool {name!r}")


def main() -> None:
    mode = os.environ.get("FAKE_MCP_MODE", "normal")

    for line in sys.stdin:
        stripped = line.strip()
        if not stripped:
            continue
        message = json.loads(stripped)
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            if mode == "no_version":
                _result(request_id, {"capabilities": {}})
            else:
                _result(
                    request_id,
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "1"},
                    },
                )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            if mode == "hostile_schema":
                _result(
                    request_id,
                    {
                        "tools": [
                            {
                                "name": "bad name with spaces",
                                "description": "rejected for its name",
                                "inputSchema": {"type": "object"},
                            },
                            {
                                "name": "unsupported",
                                "description": "rejected for its type",
                                "inputSchema": {"type": "function"},
                            },
                            {
                                "name": "echo",
                                "description": "d" * 5000,
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"],
                                    "additionalProperties": True,
                                },
                            },
                        ]
                    },
                )
            elif mode == "huge_tool":
                _result(
                    request_id,
                    {
                        "tools": [
                            {
                                "name": "huge",
                                "description": "Return a lot of text.",
                                "inputSchema": {"type": "object", "properties": {}},
                            }
                        ]
                    },
                )
            else:
                _result(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            _call(request_id, message.get("params") or {}, mode)
        elif request_id is not None:
            _error(request_id, f"unknown method {method!r}")


if __name__ == "__main__":
    main()
