from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from truecoder.client.response import TokenUsage


class AgentEventType(str, Enum):
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    AGENT_ERROR = "agent_error"
    TEXT_DELTA = "text_delta"
    TEXT_COMPLETE = "text_complete"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """A high-level event emitted while an agent turn is running."""

    type: AgentEventType
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def agent_start(cls, message: str) -> AgentEvent:
        return cls(
            type=AgentEventType.AGENT_START,
            data={"message": message},
        )

    @classmethod
    def agent_end(
        cls,
        response: str,
        usage: TokenUsage | None = None,
        finish_reason: str | None = None,
    ) -> AgentEvent:
        return cls(
            type=AgentEventType.AGENT_END,
            data={
                "response": response,
                "usage": asdict(usage) if usage else None,
                "finish_reason": finish_reason,
            },
        )

    @classmethod
    def agent_error(
        cls,
        error: str,
        details: dict[str, Any] | None = None,
    ) -> AgentEvent:
        return cls(
            type=AgentEventType.AGENT_ERROR,
            data={"error": error, "details": details or {}},
        )

    @classmethod
    def text_delta(cls, content: str) -> AgentEvent:
        return cls(
            type=AgentEventType.TEXT_DELTA,
            data={"content": content},
        )

    @classmethod
    def text_complete(cls, content: str) -> AgentEvent:
        return cls(
            type=AgentEventType.TEXT_COMPLETE,
            data={"content": content},
        )

    @classmethod
    def tool_call(cls, call_id: str, name: str, arguments: str) -> AgentEvent:
        return cls(
            type=AgentEventType.TOOL_CALL,
            data={"call_id": call_id, "name": name, "arguments": arguments},
        )

    @classmethod
    def tool_result(
        cls,
        call_id: str,
        tool_name: str,
        status: str,
        content: str,
    ) -> AgentEvent:
        return cls(
            type=AgentEventType.TOOL_RESULT,
            data={
                "call_id": call_id,
                "tool_name": tool_name,
                "status": status,
                "content": content,
            },
        )
