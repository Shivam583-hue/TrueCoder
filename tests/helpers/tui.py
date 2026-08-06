from __future__ import annotations

from collections.abc import Callable
from time import monotonic

from textual.pilot import Pilot

WAIT_TIMEOUT_SECONDS = 10.0


async def wait_until(
    pilot: Pilot,
    predicate: Callable[[], bool],
    *,
    description: str,
    timeout: float = WAIT_TIMEOUT_SECONDS,
) -> None:
    """Pump the app until ``predicate`` holds.

    ``pilot.pause()`` returns once the process looks idle, which it measures by
    comparing process time against wall clock. A task that has not been
    scheduled yet looks exactly as idle as one that has finished, so a single
    pause never establishes that a worker reached any particular point. State
    produced by a worker has to be waited on directly.
    """
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError(
                f"timed out after {timeout} seconds waiting for {description}"
            )
        await pilot.pause()
