from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from truecoder.mcp.schema import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    SchemaRejected,
    bound_text,
    bound_tool_schema,
)

MAX_TOOLS_PER_SERVER: Final = 64
MAX_RESULT_CHARACTERS: Final = 64 * 1024

_ALLOWED_NAME_CHARACTERS: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    name: str
    description: str
    schema: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("a tool name is required")
        if not isinstance(self.description, str):
            raise TypeError("a tool description must be text")
        if not isinstance(self.schema, dict):
            raise TypeError("a tool schema must be a JSON object")


@dataclass(frozen=True, slots=True)
class McpToolResult:
    text: str
    is_error: bool = False
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("a tool result must be text")


def usable_tool_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and bool(name.strip())
        and len(name) <= MAX_NAME_LENGTH
        and set(name) <= _ALLOWED_NAME_CHARACTERS
    )


def parse_tool_descriptors(payload: object) -> tuple[McpToolDescriptor, ...]:
    if not isinstance(payload, dict):
        return ()

    listed = payload.get("tools")
    if not isinstance(listed, list):
        return ()

    descriptors: list[McpToolDescriptor] = []
    for entry in listed[:MAX_TOOLS_PER_SERVER]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not usable_tool_name(name):
            continue
        assert isinstance(name, str)
        raw_schema = entry.get("inputSchema", entry.get("input_schema"))
        try:
            schema = bound_tool_schema(raw_schema)
        except SchemaRejected:
            continue
        descriptors.append(
            McpToolDescriptor(
                name=name,
                description=bound_text(
                    entry.get("description"),
                    MAX_DESCRIPTION_LENGTH,
                ),
                schema=schema,
            )
        )
    return tuple(descriptors)


def parse_tool_result(payload: object) -> McpToolResult:
    if not isinstance(payload, dict):
        return McpToolResult(text="", is_error=True)

    is_error = bool(payload.get("isError", payload.get("is_error", False)))
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        return McpToolResult(text="", is_error=is_error)

    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(kind, str):
            parts.append(f"[{kind} content omitted]")

    joined = "\n".join(parts)
    if len(joined) > MAX_RESULT_CHARACTERS:
        return McpToolResult(
            text=joined[:MAX_RESULT_CHARACTERS],
            is_error=is_error,
            truncated=True,
        )
    return McpToolResult(text=joined, is_error=is_error)
