from __future__ import annotations

from dataclasses import dataclass

from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.models import ExecutionContext


@dataclass(frozen=True, slots=True)
class ToolInvocationContext:
    execution: ExecutionContext
    cancellation_source: CancellationSource

    def __post_init__(self) -> None:
        if not isinstance(self.execution, ExecutionContext):
            raise TypeError("execution must be an ExecutionContext")
        if not isinstance(self.cancellation_source, CancellationSource):
            raise TypeError("cancellation_source must be a CancellationSource")
