"""One prompt, no interface, and an exit code that means something."""

from __future__ import annotations

import io
import unittest
from unittest.mock import AsyncMock, Mock, patch

from truecoder.agent.autonomy import Autonomy, UnattendedApprovals
from truecoder.agent.events import AgentEvent
from truecoder.cli import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    build_parser,
    main,
    render_event,
    run_prompt,
)


class _Agent:
    def __init__(self, events):
        self._events = events
        self.prompts: list[str] = []

    async def run(self, prompt):
        self.prompts.append(prompt)
        for event in self._events:
            yield event


class ParserTests(unittest.TestCase):
    def test_no_prompt_means_interactive(self):
        self.assertIsNone(build_parser().parse_args([]).prompt)

    def test_the_default_autonomy_is_read_only(self):
        parsed = build_parser().parse_args(["-p", "hello"])

        self.assertEqual(parsed.autonomy, Autonomy.READ_ONLY.value)

    def test_every_flag_parses(self):
        parsed = build_parser().parse_args(
            ["-p", "hi", "--autonomy", "edit", "--max-iterations", "3", "--quiet"]
        )

        self.assertEqual(parsed.prompt, "hi")
        self.assertEqual(parsed.autonomy, "edit")
        self.assertEqual(parsed.max_iterations, 3)
        self.assertTrue(parsed.quiet)


class UsageTests(unittest.TestCase):
    def test_an_empty_prompt_is_a_usage_error(self):
        self.assertEqual(main(["-p", "   "]), EXIT_USAGE)

    def test_an_unknown_autonomy_is_a_usage_error(self):
        self.assertEqual(main(["-p", "hi", "--autonomy", "yolo"]), EXIT_USAGE)

    def test_a_zero_iteration_budget_is_a_usage_error(self):
        self.assertEqual(main(["-p", "hi", "--max-iterations", "0"]), EXIT_USAGE)

    def test_no_prompt_launches_the_interface(self):
        with patch("truecoder.agent.agent.run_interactive") as launch:
            code = main([])

        launch.assert_called_once_with()
        self.assertEqual(code, EXIT_OK)


class RenderTests(unittest.TestCase):
    def _render(self, event, *, quiet=False) -> str:
        stream = io.StringIO()
        render_event(event, quiet=quiet, stream=stream)
        return stream.getvalue()

    def test_a_tool_call_is_announced(self):
        output = self._render(AgentEvent.tool_call("call_1", "read_file", "{}"))

        self.assertIn("read_file", output)

    def test_quiet_hides_tool_calls(self):
        output = self._render(
            AgentEvent.tool_call("call_1", "read_file", "{}"),
            quiet=True,
        )

        self.assertEqual(output, "")

    def test_a_failed_tool_is_reported(self):
        output = self._render(
            AgentEvent.tool_result("call_1", "shell", "error", "it broke")
        )

        self.assertIn("shell", output)
        self.assertIn("it broke", output)

    def test_an_agent_error_is_reported_even_when_quiet(self):
        output = self._render(AgentEvent.agent_error("no model"), quiet=True)

        self.assertIn("no model", output)


class RunPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_final_reply_is_returned(self):
        agent = _Agent([AgentEvent.agent_end("all done", None, "stop")])

        reply, failed = await run_prompt(
            agent,
            "do it",
            quiet=True,
            stream=io.StringIO(),
        )

        self.assertEqual(reply, "all done")
        self.assertFalse(failed)
        self.assertEqual(agent.prompts, ["do it"])

    async def test_an_agent_error_marks_the_run_failed(self):
        agent = _Agent(
            [
                AgentEvent.agent_error("broken"),
                AgentEvent.agent_end("", None, "error"),
            ]
        )

        _reply, failed = await run_prompt(
            agent,
            "do it",
            quiet=True,
            stream=io.StringIO(),
        )

        self.assertTrue(failed)


class HeadlessExitTests(unittest.IsolatedAsyncioTestCase):
    def _session(self, events, handler_sink):
        agent = Mock()
        agent.run = _Agent(events).run
        agent.initialize_execution = AsyncMock(return_value=None)
        agent.initialize_mcp = AsyncMock(return_value=())
        session = Mock()
        session.agent = agent
        session.close = AsyncMock()
        handler_sink.append(agent)
        return session

    def test_a_successful_run_exits_zero(self):
        agents: list = []
        with patch(
            "truecoder.agent.agent.build_session",
            side_effect=lambda **_: self._session(
                [AgentEvent.agent_end("done", None, "stop")], agents
            ),
        ):
            self.assertEqual(main(["-p", "hi", "--quiet"]), EXIT_OK)

    def test_a_failed_run_exits_nonzero(self):
        agents: list = []
        with patch(
            "truecoder.agent.agent.build_session",
            side_effect=lambda **_: self._session(
                [
                    AgentEvent.agent_error("nope"),
                    AgentEvent.agent_end("", None, "error"),
                ],
                agents,
            ),
        ):
            self.assertEqual(main(["-p", "hi", "--quiet"]), EXIT_FAILED)

    def test_the_session_is_closed_even_when_the_turn_fails(self):
        agents: list = []
        sessions: list = []

        def build(**_):
            session = self._session([AgentEvent.agent_error("nope")], agents)
            sessions.append(session)
            return session

        with patch("truecoder.agent.agent.build_session", side_effect=build):
            main(["-p", "hi", "--quiet"])

        sessions[0].close.assert_awaited_once()


class RefusalReportTests(unittest.TestCase):
    def test_refusals_are_printed_for_the_operator(self):
        handler = UnattendedApprovals(Autonomy.READ_ONLY)
        handler.refused.append(("shell", "needs a person"))
        stream = io.StringIO()

        from truecoder.cli import _report_refusals

        _report_refusals(handler, stream)

        self.assertIn("shell", stream.getvalue())
        self.assertIn("needs a person", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
