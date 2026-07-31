from __future__ import annotations

import asyncio


class CancellationRequested(Exception):
    """Raised when controlled work observes a cancellation request."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class CancellationToken:
    """Read-only cancellation state shared with execution workers."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    async def wait(self) -> str:
        await self._event.wait()
        if self._reason is None:
            raise RuntimeError("cancelled token is missing its reason")
        return self._reason

    def raise_if_cancelled(self) -> None:
        if self._reason is not None:
            raise CancellationRequested(self._reason)

    def _cancel(self, reason: str) -> bool:
        if self._event.is_set():
            return False
        self._reason = reason
        self._event.set()
        return True


class CancellationSource:
    """Own the authority to request cancellation exactly once."""

    def __init__(self) -> None:
        self._token = CancellationToken()

    @property
    def token(self) -> CancellationToken:
        return self._token

    def cancel(self, reason: str) -> bool:
        if not isinstance(reason, str):
            raise TypeError("cancellation reason must be a string")
        normalized = reason.strip()
        if not normalized:
            raise ValueError("cancellation reason cannot be empty")
        return self._token._cancel(normalized)
