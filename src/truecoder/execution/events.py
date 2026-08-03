from __future__ import annotations

import asyncio
from collections import deque
from typing import Final, Protocol, runtime_checkable

from truecoder.execution.clock import Clock, validate_clock
from truecoder.execution.models import (
    EXECUTION_LIFECYCLE_STAGES,
    ExecutionLifecycleEvent,
    ExecutionLifecycleStage,
)

DEFAULT_EVENT_CAPACITY: Final = 64

TERMINAL_STAGES: Final = frozenset(
    {
        "completed",
        "failed",
        "timed_out",
        "cancelled",
        "denied",
        "limit_exceeded",
        "failed_to_start",
    }
)


@runtime_checkable
class ExecutionEventSink(Protocol):
    async def publish(self, event: ExecutionLifecycleEvent) -> None: ...


class NullEventSink:
    async def publish(self, event: ExecutionLifecycleEvent) -> None:
        del event


class LifecyclePublisher:
    def __init__(
        self,
        execution_id: str,
        sink: ExecutionEventSink,
        clock: Clock,
        *,
        capacity: int = DEFAULT_EVENT_CAPACITY,
    ) -> None:
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise ValueError("execution_id cannot be empty")
        if not isinstance(sink, ExecutionEventSink):
            raise TypeError("sink must implement ExecutionEventSink")
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise TypeError("capacity must be an integer")
        if capacity <= 0:
            raise ValueError("capacity must be greater than zero")

        self._execution_id = execution_id.strip()
        self._sink = sink
        self._clock = validate_clock(clock)
        self._capacity = capacity
        self._pending: deque[ExecutionLifecycleEvent] = deque()
        self._terminal: ExecutionLifecycleEvent | None = None
        self._terminal_claimed = False
        self._wakeup = asyncio.Event()
        self._closed = False
        self._sequence = 0
        self._worker: asyncio.Task | None = None
        self.dropped = 0
        self.delivered = 0
        self.failures = 0

    @property
    def buffered(self) -> int:
        return len(self._pending) + (1 if self._terminal is not None else 0)

    @property
    def terminal_published(self) -> bool:
        return self._terminal_claimed

    def start(self) -> asyncio.Task:
        if self._worker is None:
            self._worker = asyncio.create_task(self._drain())
        return self._worker

    async def publish(
        self,
        stage: ExecutionLifecycleStage,
        *,
        message: str | None = None,
        details: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if stage not in EXECUTION_LIFECYCLE_STAGES:
            raise ValueError(f"unknown execution lifecycle stage: {stage!r}")
        if self._closed:
            return

        terminal = stage in TERMINAL_STAGES
        if terminal and self._terminal_claimed:
            return

        event = ExecutionLifecycleEvent(
            execution_id=self._execution_id,
            stage=stage,
            occurred_at_utc=self._clock.now_utc(),
            sequence=self._sequence,
            message=message,
            details=details,
        )
        self._sequence += 1

        if terminal:
            self._terminal = event
            self._terminal_claimed = True
        elif len(self._pending) >= self._capacity:
            self._pending.popleft()
            self._pending.append(event)
            self.dropped += 1
        else:
            self._pending.append(event)

        self._wakeup.set()
        self.start()
        await asyncio.sleep(0)

    async def aclose(self, *, drain_timeout: float = 0.5) -> None:
        self._closed = True
        self._wakeup.set()
        worker = self._worker
        if worker is None:
            return

        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=drain_timeout)
        except TimeoutError:
            worker.cancel()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            self._worker = None

    async def _drain(self) -> None:
        while True:
            if not self._pending and self._terminal is None:
                if self._closed:
                    return
                self._wakeup.clear()
                await self._wakeup.wait()
                continue

            if self._pending:
                event = self._pending.popleft()
            else:
                assert self._terminal is not None
                event = self._terminal
                self._terminal = None

            try:
                await self._sink.publish(event)
                self.delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.failures += 1
