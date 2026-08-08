from __future__ import annotations

from enum import Enum
from typing import Final

from truecoder.execution.approval import ApprovalRequest, ApprovalResponse
from truecoder.execution.models import RiskLevel

_RISK_RANK: Final = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


class Autonomy(str, Enum):
    READ_ONLY = "read-only"
    EDIT = "edit"
    FULL = "full"


AUTONOMY_CEILINGS: Final = {
    Autonomy.READ_ONLY: RiskLevel.LOW,
    Autonomy.EDIT: RiskLevel.MEDIUM,
    Autonomy.FULL: RiskLevel.HIGH,
}


def autonomy_from_name(name: str) -> Autonomy:
    try:
        return Autonomy(name)
    except ValueError:
        allowed = ", ".join(level.value for level in Autonomy)
        raise ValueError(f"unknown autonomy level {name!r}; expected one of {allowed}")


def refusal_reason(request: ApprovalRequest, autonomy: Autonomy) -> str | None:
    if request.mutation is not None and autonomy is Autonomy.READ_ONLY:
        return "changing a file needs a person at read-only autonomy"

    execution = request.execution
    if execution is None:
        return None

    if autonomy is Autonomy.READ_ONLY:
        return "running a command needs a person at read-only autonomy"

    ceiling = AUTONOMY_CEILINGS[autonomy]
    if _RISK_RANK[execution.risk] > _RISK_RANK[ceiling]:
        return (
            f"{execution.risk.value} risk is above the {autonomy.value} "
            f"ceiling of {ceiling.value}"
        )
    return None


class UnattendedApprovals:
    def __init__(self, autonomy: Autonomy = Autonomy.READ_ONLY) -> None:
        if not isinstance(autonomy, Autonomy):
            raise TypeError("autonomy must be an Autonomy level")

        self.autonomy = autonomy
        self.approved: list[str] = []
        self.refused: list[tuple[str, str]] = []

    async def __call__(self, request: ApprovalRequest) -> ApprovalResponse:
        reason = refusal_reason(request, self.autonomy)
        if reason is not None:
            self.refused.append((request.tool_name, reason))
            return ApprovalResponse.reject()

        self.approved.append(request.tool_name)
        return ApprovalResponse.approve()
