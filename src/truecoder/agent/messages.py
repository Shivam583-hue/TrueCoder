from collections.abc import Sequence
from copy import deepcopy
from typing import Literal, TypeAlias, TypedDict

from truecoder.tools.base import ToolCall


class SystemMessage(TypedDict):
    role: Literal["system"]
    content: str


class UserMessage(TypedDict):
    role: Literal["user"]
    content: str


class AssistantTextMessage(TypedDict):
    role: Literal["assistant"]
    content: str


class FunctionCallPayload(TypedDict):
    name: str
    arguments: str


class AssistantFunctionToolCall(TypedDict):
    id: str
    type: Literal["function"]
    function: FunctionCallPayload


class AssistantToolCallMessage(TypedDict):
    role: Literal["assistant"]
    content: str | None
    tool_calls: list[AssistantFunctionToolCall]


class ToolMessage(TypedDict):
    role: Literal["tool"]
    tool_call_id: str
    content: str


ModelMessage: TypeAlias = (
    SystemMessage
    | UserMessage
    | AssistantTextMessage
    | AssistantToolCallMessage
    | ToolMessage
)


def create_system_message(content: str) -> SystemMessage:
    return {
        "role": "system",
        "content": content,
    }


def create_user_message(content: str) -> UserMessage:
    return {
        "role": "user",
        "content": content,
    }


def create_assistant_text_message(content: str) -> AssistantTextMessage:
    return {
        "role": "assistant",
        "content": content,
    }


def create_assistant_tool_call(
    tool_call: ToolCall,
) -> AssistantFunctionToolCall:
    return {
        "id": tool_call.call_id,
        "type": "function",
        "function": {
            "name": tool_call.name,
            # Preserve the original raw JSON exactly.
            "arguments": tool_call.arguments_json,
        },
    }


def create_assistant_tool_call_message(
    tool_calls: Sequence[ToolCall],
    content: str | None = None,
) -> AssistantToolCallMessage:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            create_assistant_tool_call(tool_call) for tool_call in tool_calls
        ],
    }


def create_tool_message(
    tool_call_id: str,
    content: str,
) -> ToolMessage:
    """
    `content` must already be serialized by the caller.
    """
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content,
    }


def copy_message(message: ModelMessage) -> ModelMessage:
    """Copy a message when transferring ownership."""
    return deepcopy(message)


def copy_messages(
    messages: Sequence[ModelMessage],
) -> list[ModelMessage]:
    """Copy a message collection when transferring ownership."""
    return deepcopy(list(messages))
