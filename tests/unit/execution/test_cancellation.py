from __future__ import annotations

import asyncio
import unittest

from truecoder.execution.cancellation import (
    CancellationRequested,
    CancellationSource,
)


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_request_wins_and_later_requests_are_idempotent(self):
        source = CancellationSource()

        self.assertTrue(source.cancel("user"))
        self.assertFalse(source.cancel("shutdown"))

        self.assertTrue(source.token.cancelled)
        self.assertEqual(source.token.reason, "user")
        self.assertEqual(await source.token.wait(), "user")

    async def test_cancellation_before_wait_is_observed(self):
        source = CancellationSource()
        source.cancel("turn cancelled")

        reason = await asyncio.wait_for(source.token.wait(), timeout=0.1)

        self.assertEqual(reason, "turn cancelled")

    async def test_all_waiters_are_released(self):
        source = CancellationSource()
        waiters = tuple(asyncio.create_task(source.token.wait()) for _ in range(3))
        await asyncio.sleep(0)

        source.cancel("shutdown")
        reasons = await asyncio.gather(*waiters)

        self.assertEqual(reasons, ["shutdown", "shutdown", "shutdown"])

    async def test_raise_if_cancelled_preserves_the_reason(self):
        source = CancellationSource()
        source.token.raise_if_cancelled()
        source.cancel("user")

        with self.assertRaisesRegex(CancellationRequested, "user") as raised:
            source.token.raise_if_cancelled()

        self.assertEqual(raised.exception.reason, "user")

    async def test_rejects_invalid_reasons(self):
        source = CancellationSource()

        with self.assertRaises(TypeError):
            source.cancel(1)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            source.cancel("   ")
        self.assertFalse(source.token.cancelled)


if __name__ == "__main__":
    unittest.main()
