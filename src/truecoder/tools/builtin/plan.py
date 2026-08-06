from __future__ import annotations

from typing import TypedDict

from pydantic import Field

from truecoder.planning import (
    MAX_PLAN_STEPS,
    MAX_STEP_TITLE_LENGTH,
    PlanStep,
    PlanStepStatus,
    PlanStore,
)
from truecoder.tools.base import (
    BaseTool,
    ToolApproval,
    ToolArguments,
    ToolExecutionError,
)
from truecoder.tools.context import ToolInvocationContext


class PlanStepArgument(ToolArguments):
    title: str = Field(
        min_length=1,
        max_length=MAX_STEP_TITLE_LENGTH,
        description="One short imperative step, for example 'Fix the parser'.",
    )
    status: PlanStepStatus = Field(
        description=(
            "Use 'in_progress' for the single step being worked on right now, "
            "'done' for finished steps, and 'pending' for the rest."
        ),
    )


class UpdatePlanArguments(ToolArguments):
    steps: list[PlanStepArgument] = Field(
        min_length=1,
        max_length=MAX_PLAN_STEPS,
        description="The complete ordered plan. This replaces any previous plan.",
    )


class UpdatePlanOutput(TypedDict):
    total: int
    completed: int
    in_progress: str | None


class UpdatePlanTool(BaseTool[UpdatePlanArguments]):
    name = "update_plan"
    description = (
        "Record or revise the plan for a multi-step task. Always send the "
        "complete ordered list of steps; it replaces the previous plan. At most "
        "one step may be 'in_progress'."
    )
    arguments_type = UpdatePlanArguments
    approval = ToolApproval.NOT_REQUIRED

    def __init__(self, store: PlanStore) -> None:
        if not isinstance(store, PlanStore):
            raise TypeError("store must be a PlanStore.")

        self._store = store

    @property
    def store(self) -> PlanStore:
        return self._store

    async def run(
        self,
        arguments: UpdatePlanArguments,
        invocation: ToolInvocationContext | None = None,
    ) -> UpdatePlanOutput:
        del invocation

        try:
            steps = [
                PlanStep(title=step.title, status=step.status)
                for step in arguments.steps
            ]
            plan = self._store.replace(steps)
        except (TypeError, ValueError) as error:
            raise ToolExecutionError(str(error), code="invalid_plan") from error

        active = plan.active_step
        return {
            "total": plan.total,
            "completed": plan.completed,
            "in_progress": None if active is None else active.title,
        }
