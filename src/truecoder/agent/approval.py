from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    call_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


ApprovalHandler = Callable[[ApprovalRequest], Awaitable[ApprovalDecision]]


async def reject_all_tool_calls(_request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision.REJECTED


async def approve_all_tool_calls(_request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision.APPROVED
