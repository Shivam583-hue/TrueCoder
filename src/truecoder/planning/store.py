from __future__ import annotations

from collections.abc import Sequence

from truecoder.planning.models import Plan, PlanStep


class PlanStore:
    def __init__(self) -> None:
        self._current: Plan | None = None

    @property
    def current(self) -> Plan | None:
        return self._current

    def replace(self, steps: Sequence[PlanStep]) -> Plan:
        plan = Plan(tuple(steps))
        self._current = plan
        return plan

    def clear(self) -> None:
        self._current = None
