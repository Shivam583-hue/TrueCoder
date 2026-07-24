from __future__ import annotations

from typing import Any

from truecoder.tools.base import BaseTool


class DuplicateToolError(ValueError):
    """Raised when a tool name is already registered."""


class ToolNotFoundError(LookupError):
    """Raised when a requested tool is not registered."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool[Any]] = {}

    def register(self, tool: BaseTool[Any]) -> None:

        name = tool.name

        if name in self._tools:
            raise DuplicateToolError(f"A tool named '{name}' is already registered.")

        self._tools[name] = tool

    def get(self, name: str) -> BaseTool[Any]:

        try:
            return self._tools[name]
        except KeyError as error:
            raise ToolNotFoundError(f"No tool named '{name}' is registered.") from error

    def all(self) -> tuple[BaseTool[Any], ...]:

        return tuple(self._tools.values())

    def definitions(self) -> list[dict[str, Any]]:

        return [
            tool.definition().to_chat_completion_schema()
            for tool in self._tools.values()
        ]

    def __contains__(self, name: str) -> bool:

        return name in self._tools
