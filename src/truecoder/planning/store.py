from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from truecoder.planning.models import Plan, PlanStep


@runtime_checkable
class PlanEventSink(Protocol):
    def publish(self, plan: Plan | None) -> None: ...


class PlanStore:
    def __init__(self, sink: PlanEventSink | None = None) -> None:
        if sink is not None and not isinstance(sink, PlanEventSink):
            raise TypeError("sink must implement PlanEventSink.")

        self._sink = sink
        self._current: Plan | None = None

    @property
    def current(self) -> Plan | None:
        return self._current

    def attach_sink(self, sink: PlanEventSink) -> None:
        if not isinstance(sink, PlanEventSink):
            raise TypeError("sink must implement PlanEventSink.")
        self._sink = sink

    def replace(self, steps: Sequence[PlanStep]) -> Plan:
        plan = Plan(tuple(steps))
        self._current = plan
        self._publish(plan)
        return plan

    def clear(self) -> None:
        if self._current is None:
            return
        self._current = None
        self._publish(None)

    def _publish(self, plan: Plan | None) -> None:
        if self._sink is not None:
            self._sink.publish(plan)
