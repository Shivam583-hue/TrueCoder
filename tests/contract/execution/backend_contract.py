from __future__ import annotations

import unittest
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from truecoder.execution.audit.models import BackendResourceIdentifier
from truecoder.execution.backends.base import (
    BackendResourceRegistrar,
    BackendStartContext,
    ExecutionBackend,
    ExecutionHandle,
)
from truecoder.execution.backends.models import (
    BackendDescriptor,
    BackendExit,
    BackendOutputChunk,
    CleanupResult,
)
from truecoder.execution.cancellation import CancellationRequested, CancellationToken
from truecoder.execution.errors import ExecutionInfrastructureError
from truecoder.execution.models import ExecutionRequest, TerminationReason
from truecoder.execution.preparation import PreparedExecution


@dataclass(slots=True)
class BackendContractTracker:
    live_resources: int = 0
    native_waits: int = 0
    native_terminations: int = 0
    native_cleanups: int = 0
    partial_start_cleanups: int = 0
    resource_registrations: int = 0
    registered_resource: BackendResourceIdentifier | None = None
    lifecycle_events: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BackendContractCase:
    backend: ExecutionBackend
    prepared: PreparedExecution
    request: ExecutionRequest
    context: BackendStartContext
    cancellation: CancellationToken
    tracker: BackendContractTracker
    expected_output: tuple[BackendOutputChunk, ...]
    expected_exit: BackendExit
    register_resource: Callable[[BackendResourceIdentifier], Awaitable[None]]


class TrackingHandle:
    def __init__(
        self,
        inner: ExecutionHandle,
        tracker: BackendContractTracker,
    ) -> None:
        self._inner = inner
        self._tracker = tracker
        self._waited = False
        self._terminated = False
        self._cleaned = False

    @property
    def execution_id(self) -> str:
        return self._inner.execution_id

    @property
    def resource(self) -> BackendResourceIdentifier:
        return self._inner.resource

    def output(self) -> AsyncIterator[BackendOutputChunk]:
        return self._inner.output()

    async def wait(self) -> BackendExit:
        if not self._waited:
            self._tracker.native_waits += 1
            self._waited = True
        return await self._inner.wait()

    async def terminate(
        self,
        reason: TerminationReason,
        grace_seconds: float,
    ) -> None:
        if not self._terminated:
            self._tracker.native_terminations += 1
            self._terminated = True
        await self._inner.terminate(reason, grace_seconds)

    async def cleanup(self) -> CleanupResult:
        if not self._cleaned:
            self._tracker.native_cleanups += 1
            self._tracker.live_resources -= 1
            self._cleaned = True
        return await self._inner.cleanup()


class TrackingBackend:
    def __init__(
        self,
        inner: ExecutionBackend,
        tracker: BackendContractTracker,
    ) -> None:
        self._inner = inner
        self._tracker = tracker

    @property
    def descriptor(self) -> BackendDescriptor:
        return self._inner.descriptor

    async def start(
        self,
        prepared: PreparedExecution,
        request: ExecutionRequest,
        context: BackendStartContext,
        cancellation: CancellationToken,
        register_resource: BackendResourceRegistrar,
    ) -> ExecutionHandle:
        try:
            handle = await self._inner.start(
                prepared,
                request,
                context,
                cancellation,
                register_resource,
            )
        except BaseException:
            if not cancellation.cancelled:
                self._tracker.partial_start_cleanups += 1
            raise
        self._tracker.live_resources += 1
        self._tracker.lifecycle_events.append("released")
        return TrackingHandle(handle, self._tracker)


class BackendContractMixin:
    """Behavior every concrete execution backend must preserve.

    A concrete unittest class mixes this in and supplies the two factory
    methods. Future POSIX, Windows, and container suites can reuse these tests
    without inheriting implementation details from the in-memory fake.
    """

    async def make_backend_case(
        self,
        *,
        exit_code: int = 0,
    ) -> BackendContractCase:
        raise NotImplementedError

    async def make_failing_start_case(
        self,
        *,
        cancelled: bool,
    ) -> BackendContractCase:
        raise NotImplementedError

    async def start_case(
        self,
        case: BackendContractCase,
    ) -> ExecutionHandle:
        return await case.backend.start(
            case.prepared,
            case.request,
            case.context,
            case.cancellation,
            case.register_resource,
        )

    async def test_backend_and_handle_satisfy_runtime_protocols(self):
        case = await self.make_backend_case()

        self.assertIsInstance(case.backend, ExecutionBackend)
        handle = await self.start_case(case)
        self.assertIsInstance(handle, ExecutionHandle)
        await handle.cleanup()

    async def test_successful_start_transfers_exact_resource_identity(self):
        case = await self.make_backend_case()

        handle = await self.start_case(case)

        self.assertEqual(case.tracker.resource_registrations, 1)
        self.assertEqual(case.tracker.registered_resource, handle.resource)
        self.assertLess(
            case.tracker.lifecycle_events.index("registered"),
            case.tracker.lifecycle_events.index("released"),
        )
        self.assertEqual(handle.execution_id, case.context.execution_id)
        self.assertEqual(handle.resource.backend, case.backend.descriptor.name)
        self.assertEqual(handle.resource.resource_id, case.context.execution_id)
        self.assertEqual(case.tracker.live_resources, 1)
        await handle.cleanup()

    async def test_output_has_one_owner_and_reaches_end_of_stream(self):
        case = await self.make_backend_case()
        handle = await self.start_case(case)

        output = handle.output()
        with self.assertRaises(RuntimeError):
            handle.output()
        chunks = tuple([chunk async for chunk in output])

        self.assertEqual(chunks, case.expected_output)
        await handle.cleanup()

    async def test_wait_is_idempotent_and_observes_native_exit_once(self):
        case = await self.make_backend_case()
        handle = await self.start_case(case)

        first = await handle.wait()
        second = await handle.wait()

        self.assertEqual(first, case.expected_exit)
        self.assertIs(first, second)
        self.assertEqual(case.tracker.native_waits, 1)
        await handle.cleanup()

    async def test_nonzero_exit_is_normal_backend_data(self):
        case = await self.make_backend_case(exit_code=7)
        handle = await self.start_case(case)

        result = await handle.wait()

        self.assertEqual(result.exit_code, 7)
        self.assertIsNone(result.native_reason)
        await handle.cleanup()

    async def test_terminate_is_idempotent_and_wait_remains_stable(self):
        case = await self.make_backend_case()
        handle = await self.start_case(case)

        await handle.terminate("cancellation", 0.1)
        await handle.terminate("timeout", 0.1)
        first = await handle.wait()
        second = await handle.wait()

        self.assertEqual(case.tracker.native_terminations, 1)
        self.assertEqual(first.native_reason, "cancellation")
        self.assertIs(first, second)
        await handle.cleanup()

    async def test_cleanup_is_idempotent_and_releases_resource_once(self):
        case = await self.make_backend_case()
        handle = await self.start_case(case)

        first = await handle.cleanup()
        second = await handle.cleanup()

        self.assertTrue(first.complete)
        self.assertIs(first, second)
        self.assertEqual(case.tracker.native_cleanups, 1)
        self.assertEqual(case.tracker.live_resources, 0)

    async def test_partial_start_failure_cleans_before_raising(self):
        case = await self.make_failing_start_case(cancelled=False)

        with self.assertRaises(ExecutionInfrastructureError):
            await case.backend.start(
                case.prepared,
                case.request,
                case.context,
                case.cancellation,
                case.register_resource,
            )

        self.assertEqual(case.tracker.partial_start_cleanups, 1)
        self.assertEqual(case.tracker.live_resources, 0)

    async def test_pre_start_cancellation_never_leaks_a_resource(self):
        case = await self.make_failing_start_case(cancelled=True)

        with self.assertRaises(CancellationRequested):
            await case.backend.start(
                case.prepared,
                case.request,
                case.context,
                case.cancellation,
                case.register_resource,
            )

        self.assertEqual(case.tracker.live_resources, 0)
        self.assertEqual(case.tracker.resource_registrations, 0)

    async def test_resource_registration_failure_cleans_before_raising(self):
        case = await self.make_backend_case()

        async def reject_registration(
            resource: BackendResourceIdentifier,
        ) -> None:
            del resource
            raise RuntimeError("injected durable registration failure")

        with self.assertRaises(RuntimeError):
            await case.backend.start(
                case.prepared,
                case.request,
                case.context,
                case.cancellation,
                reject_registration,
            )

        self.assertEqual(case.tracker.live_resources, 0)
        self.assertEqual(case.tracker.partial_start_cleanups, 1)
        self.assertNotIn("released", case.tracker.lifecycle_events)


BackendContractTestCase = unittest.IsolatedAsyncioTestCase
