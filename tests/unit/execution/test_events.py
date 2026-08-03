from __future__ import annotations

import unittest

from tests.fakes.execution import CollectingEventSink, FakeClock
from truecoder.execution.events import LifecyclePublisher


class LifecyclePublisherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.sink = CollectingEventSink()

    def publisher(self, **kwargs) -> LifecyclePublisher:
        return LifecyclePublisher(
            "exec-events",
            kwargs.pop("sink", self.sink),
            self.clock,
            **kwargs,
        )

    async def test_events_are_delivered_in_order_with_dense_sequences(self):
        publisher = self.publisher()

        for stage in ("requested", "policy_evaluated", "starting", "started"):
            await publisher.publish(stage)
        await publisher.aclose()

        self.assertEqual(
            self.sink.stages(),
            ("requested", "policy_evaluated", "starting", "started"),
        )
        self.assertEqual(
            tuple(event.sequence for event in self.sink.events),
            (0, 1, 2, 3),
        )

    async def test_saturation_drops_oldest_transient_events(self):
        publisher = self.publisher(capacity=2, sink=CollectingEventSink(block=True))

        for _index in range(6):
            await publisher.publish("starting")

        self.assertLessEqual(publisher.buffered, 3)
        self.assertGreater(publisher.dropped, 0)
        await publisher.aclose(drain_timeout=0.01)

    async def test_the_terminal_event_survives_a_saturated_buffer(self):
        sink = CollectingEventSink(block=True)
        publisher = self.publisher(capacity=1, sink=sink)

        for _index in range(10):
            await publisher.publish("starting")
        await publisher.publish("completed")

        self.assertTrue(publisher.terminal_published)
        sink.release()
        await publisher.aclose()
        self.assertIn("completed", sink.stages())

    async def test_only_one_terminal_event_is_ever_published(self):
        publisher = self.publisher()

        await publisher.publish("completed")
        await publisher.publish("failed")
        await publisher.publish("cancelled")
        await publisher.aclose()

        terminal = [
            stage
            for stage in self.sink.stages()
            if stage in {"completed", "failed", "cancelled"}
        ]
        self.assertEqual(terminal, ["completed"])

    async def test_a_failing_sink_never_breaks_the_run(self):
        sink = CollectingEventSink(fail=True)
        publisher = self.publisher(sink=sink)

        await publisher.publish("requested")
        await publisher.publish("completed")
        await publisher.aclose()

        self.assertEqual(publisher.failures, 2)
        self.assertEqual(sink.events, [])

    async def test_a_blocked_sink_never_blocks_publishing(self):
        sink = CollectingEventSink(block=True)
        publisher = self.publisher(sink=sink)

        for stage in ("requested", "policy_evaluated", "starting"):
            await publisher.publish(stage)

        self.assertEqual(sink.events, [])
        sink.release()
        await publisher.aclose()

    async def test_unknown_stages_are_rejected(self):
        publisher = self.publisher()

        with self.assertRaises(ValueError):
            await publisher.publish("not-a-stage")  # type: ignore[arg-type]

        await publisher.aclose()

    async def test_close_is_idempotent_and_leaves_no_worker(self):
        publisher = self.publisher()

        await publisher.publish("requested")
        await publisher.aclose()
        await publisher.aclose()

        self.assertIsNone(publisher._worker)


if __name__ == "__main__":
    unittest.main()
