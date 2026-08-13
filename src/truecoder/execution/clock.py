from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Protocol, runtime_checkable

from truecoder._compat import UTC


@runtime_checkable
class Clock(Protocol):
    def now_utc(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def validate_clock(clock: object, name: str = "clock") -> Clock:
    if not isinstance(clock, Clock):
        raise TypeError(f"{name} must implement the Clock protocol")
    return clock
