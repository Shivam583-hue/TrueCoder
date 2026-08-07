from __future__ import annotations

import unittest

from tests.unit.agent.test_agent import FixedTokenCounter, ScriptedLLMClient
from tests.unit.hooks.test_runner import RecordingService, _hook
from truecoder.agent import (
    Agent,
    ApprovalIdentity,
    ApprovalRequest,
    ApprovalScope,
    ContextBuilder,
)
from truecoder.client.response import EventType, StreamEvent, TextDelta
from truecoder.execution.bootstrap import ExecutionHealthReport, ExecutionRuntime
from truecoder.hooks import HookSuite


def _reply(text: str = "done") -> list[StreamEvent]:
    return [
        StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta(text)),
        StreamEvent(type=EventType.MESSAGE_COMPLETE),
    ]


def _attach(agent: Agent, service) -> None:
    agent._execution_runtime = ExecutionRuntime(
        service=service,
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


def _agent(hooks: HookSuite | None = None, replies: int = 1) -> Agent:
    return Agent(
        llm_client=ScriptedLLMClient([_reply() for _ in range(replies)]),
        hooks=hooks,
        context_builder=ContextBuilder(
            system_prompt="test system",
            max_input_tokens=1000,
            token_counter=FixedTokenCounter(),
        ),
    )


class HookWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_turn_end_hook_runs_after_the_turn(self):
        service = RecordingService()
        agent = _agent(HookSuite(hooks=(_hook(name="format"),)))
        _attach(agent, service)

        [event async for event in agent.run("go")]

        self.assertEqual(len(service.requests), 1)
        self.assertEqual([o.hook.name for o in agent.hook_outcomes], ["format"])

    async def test_a_turn_start_hook_runs_before_the_turn(self):
        service = RecordingService()
        agent = _agent(HookSuite(hooks=(_hook(name="pre", event="turn_start"),)))
        _attach(agent, service)

        [event async for event in agent.run("go")]

        self.assertEqual(len(service.requests), 1)

    async def test_no_hooks_means_no_executions(self):
        service = RecordingService()
        agent = _agent()
        _attach(agent, service)

        [event async for event in agent.run("go")]

        self.assertEqual(service.requests, [])
        self.assertEqual(agent.hook_outcomes, ())

    async def test_an_unavailable_suite_runs_nothing(self):
        service = RecordingService()
        agent = _agent(
            HookSuite(hooks=(_hook(),), unavailable_reason="broken configuration")
        )
        _attach(agent, service)

        [event async for event in agent.run("go")]

        self.assertEqual(service.requests, [])

    async def test_hooks_are_skipped_without_an_execution_runtime(self):
        agent = _agent(HookSuite(hooks=(_hook(),)))

        events = [event async for event in agent.run("go")]

        self.assertEqual(agent.hook_outcomes, ())
        self.assertTrue(events)

    async def test_a_failing_hook_never_breaks_the_turn(self):
        service = RecordingService(error=RuntimeError("boom"))
        agent = _agent(HookSuite(hooks=(_hook(),)))
        _attach(agent, service)

        events = [event async for event in agent.run("go")]

        self.assertEqual(events[-1].data.get("response"), "done")
        self.assertEqual(agent.hook_outcomes[0].status, "failed")

    async def test_a_files_changed_hook_is_skipped_without_changes(self):
        service = RecordingService()
        agent = _agent(
            HookSuite(hooks=(_hook(condition="files_changed"),))
        )
        _attach(agent, service)

        [event async for event in agent.run("go")]

        self.assertEqual(service.requests, [])

    async def test_a_non_suite_is_rejected(self):
        with self.assertRaises(TypeError):
            _agent(object())  # type: ignore[arg-type]

    async def test_outcomes_are_cleared_between_turns(self):
        service = RecordingService()
        agent = _agent(HookSuite(hooks=(_hook(),)), replies=2)
        _attach(agent, service)
        [event async for event in agent.run("first")]
        self.assertEqual(len(agent.hook_outcomes), 1)

        agent.hooks = HookSuite()
        [event async for event in agent.run("second")]

        self.assertEqual(agent.hook_outcomes, ())


class PreAuthorisationTests(unittest.IsolatedAsyncioTestCase):
    def _request(self, call_id: str) -> ApprovalRequest:
        return ApprovalRequest.create(
            call_id=call_id,
            tool_name="shell",
            arguments={"command": "ruff format ."},
            identity=ApprovalIdentity(
                session_id="session_1",
                workspace_id="workspace_1",
            ),
        )

    async def test_a_pre_authorised_call_never_reaches_the_handler(self):
        asked: list[str] = []

        async def handler(request):
            asked.append(request.call_id)
            raise AssertionError("the user should not be asked")

        agent = _agent()
        agent.approval_handler = handler
        release = agent.pre_authorise("hook_format")

        response = await agent._invoke_approval_handler(self._request("hook_format"))

        self.assertEqual(response.scope, ApprovalScope.ONCE)
        self.assertEqual(asked, [])
        release()

    async def test_an_ordinary_call_still_reaches_the_handler(self):
        asked: list[str] = []

        async def handler(request):
            from truecoder.agent import ApprovalResponse

            asked.append(request.call_id)
            return ApprovalResponse.reject()

        agent = _agent()
        agent.approval_handler = handler
        agent.pre_authorise("hook_format")

        await agent._invoke_approval_handler(self._request("call_1"))

        self.assertEqual(asked, ["call_1"])

    async def test_releasing_removes_the_authorisation(self):
        asked: list[str] = []

        async def handler(request):
            from truecoder.agent import ApprovalResponse

            asked.append(request.call_id)
            return ApprovalResponse.reject()

        agent = _agent()
        agent.approval_handler = handler
        release = agent.pre_authorise("hook_format")
        release()

        await agent._invoke_approval_handler(self._request("hook_format"))

        self.assertEqual(asked, ["hook_format"])


if __name__ == "__main__":
    unittest.main()
