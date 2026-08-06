from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from tests.helpers.tui import wait_until
from truecoder.agent import Agent, ContextBuilder
from truecoder.agent.approval import (
    ApprovalDecision,
    ApprovalIdentity,
    ApprovalRequest,
    ApprovalScope,
)
from truecoder.execution.bootstrap import ExecutionHealthReport, ExecutionRuntime
from truecoder.execution.models import ExecutionLifecycleEvent
from truecoder.execution.registry import CancellationOutcome
from truecoder.tui.app import (
    ExecutionOutputMessage,
    ExecutionStageMessage,
    TrueCoderApp,
)
from truecoder.tui.widgets import ExecutionCard


class FakeRegistry:
    def __init__(self, execution_ids: tuple[str, ...] = ()) -> None:
        self.execution_ids = list(execution_ids)

    async def active_execution_ids(self) -> tuple[str, ...]:
        return tuple(self.execution_ids)


class FakeExecutionService:
    def __init__(self, execution_ids: tuple[str, ...] = ()) -> None:
        self.registry = FakeRegistry(execution_ids)
        self.cancellations: list[tuple[str, str]] = []
        self.outcome = CancellationOutcome.REQUESTED

    async def cancel(
        self,
        execution_id: str,
        *,
        reason: str = "user",
    ) -> CancellationOutcome:
        self.cancellations.append((execution_id, reason))
        if self.outcome is CancellationOutcome.REQUESTED:
            self.registry.execution_ids = [
                active
                for active in self.registry.execution_ids
                if active != execution_id
            ]
        return self.outcome


class FixedTokenCounter:
    def count_message(self, message) -> int:
        return 1


class IdleLLMClient:
    def __init__(self) -> None:
        self.closed = False

    async def chat_completion(self, messages, stream=True, tools=None):
        del messages, stream, tools
        return
        yield  # pragma: no cover - generator shape only

    async def close(self) -> None:
        self.closed = True


def make_agent() -> Agent:
    return Agent(
        llm_client=IdleLLMClient(),
        context_builder=ContextBuilder(
            system_prompt="test system",
            max_input_tokens=100,
            token_counter=FixedTokenCounter(),
        ),
    )


def attach_execution(agent: Agent, service: FakeExecutionService) -> None:
    agent._execution_runtime = ExecutionRuntime(
        service=service,  # type: ignore[arg-type]
        audit=None,
        discovery=None,
        backends=(),
        health=ExecutionHealthReport(
            enabled=True,
            audit_ready=True,
            recovery_ready=True,
            backends=(),
        ),
    )
    agent._execution_initialized = True


def stage(
    execution_id: str,
    name: str,
    sequence: int = 0,
    *,
    details: tuple[tuple[str, str], ...] = (),
) -> ExecutionStageMessage:
    return ExecutionStageMessage(
        ExecutionLifecycleEvent(
            execution_id=execution_id,
            stage=name,  # type: ignore[arg-type]
            occurred_at_utc=datetime.now(UTC),
            sequence=sequence,
            details=details,
        )
    )


def approval_request(
    *,
    call_id: str = "call-1",
    scopes: tuple[ApprovalScope, ...] = (ApprovalScope.ONCE,),
) -> ApprovalRequest:
    return ApprovalRequest(
        call_id=call_id,
        tool_name="shell",
        arguments_json="{}",
        identity=ApprovalIdentity(
            session_id="session-1",
            workspace_id="workspace-1",
        ),
        fingerprint="fingerprint-1",
        allowed_scopes=scopes,
    )


class ExecutionCardLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_requested_command_replaces_the_generic_card_title(self):
        app = TrueCoderApp(make_agent())

        async with app.run_test(size=(120, 40)) as pilot:
            app.post_message(
                stage(
                    "exec-1",
                    "requested",
                    details=(("command", "pytest -q"),),
                )
            )
            await pilot.pause()

            self.assertEqual(app.query_one(ExecutionCard).command, "pytest -q")

    async def test_a_card_appears_and_evolves_from_typed_stages(self):
        app = TrueCoderApp(make_agent())

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                app.post_message(stage("exec-1", "requested", 0))
                await pilot.pause()
                card = app.query_one(ExecutionCard)
                self.assertEqual(card.state, "preparing")

                app.post_message(stage("exec-1", "started", 1))
                await pilot.pause()
                self.assertEqual(card.state, "running")

                app.post_message(stage("exec-1", "completed", 2))
                await pilot.pause()
                self.assertEqual(card.state, "completed")

    async def test_output_reaches_the_card_while_it_runs(self):
        app = TrueCoderApp(make_agent())

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                app.post_message(stage("exec-1", "started", 0))
                await pilot.pause()
                app.post_message(
                    ExecutionOutputMessage("exec-1", "stdout", "building\n")
                )
                await pilot.pause()

                card = app.query_one(ExecutionCard)
                self.assertIn("building", card._preview.text())

    async def test_output_for_an_unknown_execution_is_ignored(self):
        app = TrueCoderApp(make_agent())

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                app.post_message(
                    ExecutionOutputMessage("ghost", "stdout", "orphan\n")
                )
                await pilot.pause()

                self.assertEqual(len(app.query(ExecutionCard)), 0)

    async def test_a_terminal_stage_arriving_immediately_still_settles(self):
        app = TrueCoderApp(make_agent())

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                app.post_message(stage("exec-1", "requested", 0))
                app.post_message(stage("exec-1", "started", 1))
                app.post_message(stage("exec-1", "completed", 2))
                await pilot.pause()

                card = app.query_one(ExecutionCard)
                self.assertEqual(card.state, "completed")
                self.assertNotIn("exec-1", app._execution_cards)

    async def test_a_terminal_stage_without_a_start_still_renders(self):
        app = TrueCoderApp(make_agent())

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                app.post_message(stage("exec-1", "denied", 0))
                await pilot.pause()

                self.assertEqual(app.query_one(ExecutionCard).state, "rejected")


class ExecutionCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_cancels_the_active_execution_by_id(self):
        service = FakeExecutionService(("exec-1",))
        agent = make_agent()
        app = TrueCoderApp(agent)
        attach_execution(agent, service)

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                app.post_message(stage("exec-1", "started", 0))
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()

                self.assertEqual(
                    service.cancellations,
                    [("exec-1", "stopped by the user")],
                )
                self.assertTrue(app.query_one(ExecutionCard).cancel_requested)

    async def test_a_second_stop_does_not_cancel_the_same_execution_twice(self):
        service = FakeExecutionService(("exec-1",))
        agent = make_agent()
        app = TrueCoderApp(agent)
        attach_execution(agent, service)

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                app.post_message(stage("exec-1", "started", 0))
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()

                self.assertEqual(len(service.cancellations), 1)

    async def test_the_card_cancel_button_requests_that_exact_execution(self):
        service = FakeExecutionService(("exec-1", "exec-2"))
        agent = make_agent()
        app = TrueCoderApp(agent)
        attach_execution(agent, service)

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                app.post_message(stage("exec-2", "started", 0))
                await pilot.pause()
                card = app.query_one(ExecutionCard)
                card.post_message(ExecutionCard.CancelRequested("exec-2"))
                await pilot.pause()

                self.assertEqual(
                    service.cancellations,
                    [("exec-2", "cancelled from the interface")],
                )

    async def test_stop_without_any_execution_falls_through_to_the_prompt(self):
        service = FakeExecutionService()
        agent = make_agent()
        app = TrueCoderApp(agent)
        attach_execution(agent, service)

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("escape")
                await pilot.pause()

                self.assertEqual(service.cancellations, [])
                self.assertEqual(app.focused.id, "prompt-input")

    async def test_a_cancellation_failure_never_raises_into_the_interface(self):
        service = FakeExecutionService(("exec-1",))

        async def failing_cancel(execution_id, *, reason="user"):
            raise RuntimeError("registry unavailable")

        service.cancel = failing_cancel  # type: ignore[method-assign]
        agent = make_agent()
        app = TrueCoderApp(agent)
        attach_execution(agent, service)

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                app.post_message(stage("exec-1", "started", 0))
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()

                self.assertEqual(app.query_one(ExecutionCard).state, "cancelling")


class ApprovalFocusTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_open_approval_focuses_the_approve_once_control(self):
        app = TrueCoderApp(make_agent())

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                task = asyncio.create_task(
                    app._request_tool_approval(approval_request())
                )
                await wait_until(
                    pilot,
                    lambda: app.focused is not None
                    and "approval-once" in app.focused.classes,
                    description="the open approval to focus its approve-once control",
                )

                assert app.focused is not None
                self.assertIn("approval-once", app.focused.classes)

                app._resolve_pending_approval(None)
                await task

    async def test_stop_rejects_an_open_approval_before_touching_the_turn(self):
        service = FakeExecutionService(("exec-1",))
        agent = make_agent()
        app = TrueCoderApp(agent)
        attach_execution(agent, service)

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                task = asyncio.create_task(
                    app._request_tool_approval(approval_request())
                )
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()

                response = await task
                self.assertIs(response.decision, ApprovalDecision.REJECTED)
                self.assertEqual(service.cancellations, [])

    async def test_a_scope_outside_the_allowed_set_is_refused(self):
        app = TrueCoderApp(make_agent())

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                task = asyncio.create_task(
                    app._request_tool_approval(approval_request())
                )
                await pilot.pause()

                app._resolve_pending_approval(ApprovalScope.WORKSPACE)
                await pilot.pause()
                self.assertFalse(task.done())

                app._resolve_pending_approval(ApprovalScope.ONCE)
                response = await task
                self.assertIs(response.decision, ApprovalDecision.APPROVED)


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_resolves_an_awaited_approval_instead_of_hanging(self):
        app = TrueCoderApp(make_agent())

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                task = asyncio.create_task(
                    app._request_tool_approval(approval_request())
                )
                await pilot.pause()
                self.assertFalse(task.done())

            response = await asyncio.wait_for(task, timeout=5)
            self.assertIs(response.decision, ApprovalDecision.REJECTED)

    async def test_shutdown_cancels_every_active_execution(self):
        service = FakeExecutionService(("exec-1", "exec-2"))
        agent = make_agent()
        app = TrueCoderApp(agent)
        attach_execution(agent, service)

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                app.post_message(stage("exec-1", "started", 0))
                await pilot.pause()

        self.assertEqual(
            sorted(execution_id for execution_id, _ in service.cancellations),
            ["exec-1", "exec-2"],
        )
        self.assertTrue(
            all(
                reason == "application shutting down"
                for _, reason in service.cancellations
            )
        )

    async def test_shutdown_without_execution_configured_is_still_clean(self):
        app = TrueCoderApp(make_agent())

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()

        self.assertTrue(app.agent.llm_client.closed)

    async def test_shutdown_survives_a_registry_that_cannot_be_read(self):
        service = FakeExecutionService(("exec-1",))

        async def failing_ids():
            raise RuntimeError("registry unavailable")

        service.registry.active_execution_ids = failing_ids  # type: ignore[method-assign]
        agent = make_agent()
        app = TrueCoderApp(agent)
        attach_execution(agent, service)

        with patch.dict(os.environ, {"MODEL": "test-model"}):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()

        self.assertEqual(service.cancellations, [])


if __name__ == "__main__":
    unittest.main()
