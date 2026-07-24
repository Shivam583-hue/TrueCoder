from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class ToolApproval(str, Enum):
    NOT_REQUIRED = "not_required"
    REQUIRED = "required"


class ToolResultStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    APPROVAL_REQUIRED = "approval_required"


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolArgumentError(ValueError):
    """Raised when tool arguments cannot be parsed or validated."""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = True

    def __post_init__(self) -> None:
        name = self.name.strip()
        description = self.description.strip()

        if not name:
            raise ValueError("Tool name cannot be empty.")

        if _TOOL_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(
                "Tool names may contain only letters, digits, underscores, and hyphens."
            )

        if not description:
            raise ValueError("Tool description cannot be empty.")

        if not isinstance(self.parameters, dict):
            raise TypeError("Tool parameters must be a dictionary.")

        if self.parameters.get("type") != "object":
            raise ValueError("Tool parameters must describe a JSON object.")

        if not isinstance(self.strict, bool):
            raise TypeError("Tool strict must be a Boolean.")

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(
            self,
            "parameters",
            deepcopy(self.parameters),
        )

    def to_chat_completion_schema(self) -> dict[str, Any]:
        """Return the Chat Completions function-tool schema."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": deepcopy(self.parameters),
                "strict": self.strict,
            },
        }


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A raw tool call requested by the language model."""

    call_id: str
    name: str
    arguments_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id.strip():
            raise ValueError("Tool call ID cannot be empty.")

        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Tool call name cannot be empty.")

        if not isinstance(self.arguments_json, str):
            raise TypeError("Tool arguments_json must be a string.")

        object.__setattr__(self, "call_id", self.call_id.strip())
        object.__setattr__(self, "name", self.name.strip())


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The structured result of processing a tool call."""

    call_id: str
    tool_name: str
    status: ToolResultStatus
    output: Any | None = None
    error: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id.strip():
            raise ValueError("Tool result call ID cannot be empty.")

        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("Tool result tool name cannot be empty.")

        if not isinstance(self.status, ToolResultStatus):
            raise TypeError("status must be a ToolResultStatus.")

        object.__setattr__(self, "call_id", self.call_id.strip())
        object.__setattr__(self, "tool_name", self.tool_name.strip())

        if self.status is ToolResultStatus.SUCCESS:
            if self.output is None:
                raise ValueError("A successful tool result requires output.")

            if self.error is not None:
                raise ValueError("A successful tool result cannot contain an error.")

            if self.error_code is not None:
                raise ValueError(
                    "A successful tool result cannot contain an error code."
                )

        elif self.status is ToolResultStatus.ERROR:
            if not isinstance(self.error, str) or not self.error.strip():
                raise ValueError("An error tool result requires an error message.")

            if self.output is not None:
                raise ValueError("An error tool result cannot contain output.")

            object.__setattr__(self, "error", self.error.strip())

        elif self.status is ToolResultStatus.APPROVAL_REQUIRED:
            if self.output is not None:
                raise ValueError("An approval-required result cannot contain output.")

            if self.error is not None:
                raise ValueError("An approval-required result cannot contain an error.")

    @classmethod
    def success(
        cls,
        call_id: str,
        tool_name: str,
        output: Any,
    ) -> ToolResult:
        """Create a successful tool result."""

        return cls(
            call_id=call_id,
            tool_name=tool_name,
            status=ToolResultStatus.SUCCESS,
            output=output,
        )

    @classmethod
    def failure(
        cls,
        call_id: str,
        tool_name: str,
        error: str,
        error_code: str | None = None,
    ) -> ToolResult:
        """Create an error tool result."""

        return cls(
            call_id=call_id,
            tool_name=tool_name,
            status=ToolResultStatus.ERROR,
            error=error,
            error_code=error_code,
        )

    @classmethod
    def approval_required(
        cls,
        call_id: str,
        tool_name: str,
    ) -> ToolResult:
        """Create a result indicating that user approval is required."""

        return cls(
            call_id=call_id,
            tool_name=tool_name,
            status=ToolResultStatus.APPROVAL_REQUIRED,
        )


ArgumentsT = TypeVar("ArgumentsT", bound=ToolArguments)


class BaseTool(ABC, Generic[ArgumentsT]):
    """Base class shared by all executable tools."""

    name: ClassVar[str]
    description: ClassVar[str]
    arguments_type: ClassVar[type[ArgumentsT]]
    approval: ClassVar[ToolApproval] = ToolApproval.REQUIRED

    def definition(self) -> ToolDefinition:
        """Build the definition supplied to the language model."""

        if not issubclass(self.arguments_type, ToolArguments):
            raise TypeError("arguments_type must inherit from ToolArguments.")

        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.arguments_type.model_json_schema(),
        )

    def parse_arguments(self, arguments_json: str) -> ArgumentsT:
        """Parse and validate arguments without executing the tool."""

        try:
            parsed_arguments = json.loads(arguments_json)
        except json.JSONDecodeError as error:
            raise ToolArgumentError(
                f"Arguments for tool '{self.name}' are not valid JSON."
            ) from error

        if not isinstance(parsed_arguments, dict):
            raise ToolArgumentError(
                f"Arguments for tool '{self.name}' must be a JSON object."
            )

        try:
            return self.arguments_type.model_validate(parsed_arguments)
        except ValidationError as error:
            raise ToolArgumentError(
                f"Arguments for tool '{self.name}' failed validation: {error}"
            ) from error

    @abstractmethod
    async def run(self, arguments: ArgumentsT) -> Any:
        """Execute the tool using previously validated arguments."""

        raise NotImplementedError
