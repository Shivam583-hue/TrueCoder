from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

PlanStepStatus: TypeAlias = Literal["pending", "in_progress", "done"]

PLAN_STEP_STATUSES: Final[tuple[PlanStepStatus, ...]] = (
    "pending",
    "in_progress",
    "done",
)
MAX_PLAN_STEPS: Final = 20
MAX_STEP_TITLE_LENGTH: Final = 120

STATUS_GLYPHS: Final[dict[PlanStepStatus, str]] = {
    "pending": " ",
    "in_progress": ">",
    "done": "x",
}
_WHITESPACE_RUN = re.compile(r"\s+")
_PLAN_PREAMBLE: Final = "Current plan. Keep it accurate with update_plan as you work."


def normalize_step_title(title: str) -> str:
    if not isinstance(title, str):
        raise TypeError("A plan step title must be a string.")

    normalized = _WHITESPACE_RUN.sub(" ", title).strip()

    if not normalized:
        raise ValueError("A plan step title cannot be empty.")

    if len(normalized) > MAX_STEP_TITLE_LENGTH:
        raise ValueError(
            f"A plan step title cannot exceed {MAX_STEP_TITLE_LENGTH} characters."
        )

    return normalized


@dataclass(frozen=True, slots=True)
class PlanStep:
    title: str
    status: PlanStepStatus

    def __post_init__(self) -> None:
        if self.status not in PLAN_STEP_STATUSES:
            raise ValueError(f"Unsupported plan step status: {self.status!r}")

        object.__setattr__(self, "title", normalize_step_title(self.title))


@dataclass(frozen=True, slots=True)
class Plan:
    steps: tuple[PlanStep, ...]

    def __post_init__(self) -> None:
        steps = tuple(self.steps)

        if not steps:
            raise ValueError("A plan requires at least one step.")

        if len(steps) > MAX_PLAN_STEPS:
            raise ValueError(f"A plan cannot exceed {MAX_PLAN_STEPS} steps.")

        if not all(isinstance(step, PlanStep) for step in steps):
            raise TypeError("A plan may contain only PlanStep values.")

        active = [step for step in steps if step.status == "in_progress"]
        if len(active) > 1:
            raise ValueError("Only one plan step may be in progress at a time.")

        object.__setattr__(self, "steps", steps)

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def completed(self) -> int:
        return sum(1 for step in self.steps if step.status == "done")

    @property
    def active_step(self) -> PlanStep | None:
        for step in self.steps:
            if step.status == "in_progress":
                return step
        return None

    @property
    def is_complete(self) -> bool:
        return self.completed == self.total

    def render(self) -> str:
        lines = [
            f"{position}. [{STATUS_GLYPHS[step.status]}] {step.title}"
            for position, step in enumerate(self.steps, start=1)
        ]
        return f"{_PLAN_PREAMBLE}\n\n" + "\n".join(lines)
