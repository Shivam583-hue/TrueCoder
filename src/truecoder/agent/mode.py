from __future__ import annotations

from enum import Enum
from typing import Final


class AgentMode(str, Enum):
    PLAN = "plan"
    BUILD = "build"
    FULL_ACCESS = "full-access"

    @property
    def label(self) -> str:
        return {
            AgentMode.PLAN: "Plan",
            AgentMode.BUILD: "Build",
            AgentMode.FULL_ACCESS: "Full Access",
        }[self]

    def next(self) -> AgentMode:
        return {
            AgentMode.BUILD: AgentMode.PLAN,
            AgentMode.PLAN: AgentMode.FULL_ACCESS,
            AgentMode.FULL_ACCESS: AgentMode.BUILD,
        }[self]


PLAN_TOOLS: Final = frozenset(
    {
        "find_references",
        "find_symbol",
        "get_diagnostics",
        "glob",
        "goto_definition",
        "grep",
        "list_dir",
        "read_file",
        "update_plan",
        "web_fetch",
    }
)

PLAN_AUTO_APPROVED_TOOLS: Final = PLAN_TOOLS - {"web_fetch"}


MODE_GUIDANCE: Final = {
    AgentMode.PLAN: """\
# Current mode: Plan

Investigate the request and produce a concrete implementation plan. You may read
and search the project, inspect diagnostics, consult public documentation, and
update the visible plan. Do not modify files, run commands, change memory, invoke
third-party tools, delegate work, or claim implementation is complete. If the
user asks you to implement while this mode is active, explain that Build or Full
Access mode is required.""",
    AgentMode.BUILD: """\
# Current mode: Build

Work on the request end to end using the available tools. Operations that need
the user's authorization will pause for an approval decision.""",
    AgentMode.FULL_ACCESS: """\
# Current mode: Full Access

Work on the request end to end without pausing to ask for tool approvals. This
mode does not override hard execution-policy denials, isolation requirements,
resource limits, audit recording, project boundaries, or cancellation. Continue
to avoid destructive work that the user did not request.""",
}


def mode_from_name(name: str) -> AgentMode:
    try:
        return AgentMode(name)
    except ValueError:
        allowed = ", ".join(mode.value for mode in AgentMode)
        raise ValueError(f"unknown agent mode {name!r}; expected one of {allowed}")


def mode_allows_tool(mode: AgentMode, tool_name: str) -> bool:
    if not isinstance(mode, AgentMode):
        raise TypeError("mode must be an AgentMode")
    if not isinstance(tool_name, str):
        raise TypeError("tool_name must be a string")
    return mode is not AgentMode.PLAN or tool_name in PLAN_TOOLS


def mode_auto_approves(mode: AgentMode, tool_name: str) -> bool:
    if not isinstance(mode, AgentMode):
        raise TypeError("mode must be an AgentMode")
    if not isinstance(tool_name, str):
        raise TypeError("tool_name must be a string")
    if mode is AgentMode.FULL_ACCESS:
        return True
    return mode is AgentMode.PLAN and tool_name in PLAN_AUTO_APPROVED_TOOLS
