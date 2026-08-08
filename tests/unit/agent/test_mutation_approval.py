import json
import tempfile
import unittest
from pathlib import Path

from tests.unit.agent.test_agent import (
    FixedTokenCounter,
    RecordingApprovalHandler,
    ScriptedLLMClient,
)
from truecoder.agent import Agent, ApprovalDecision, ContextBuilder
from truecoder.client.response import EventType, StreamEvent, TextDelta
from truecoder.mutation import FileDiff
from truecoder.tools import ToolCall, ToolRegistry
from truecoder.tools.builtin import EditFileTool, WriteFileTool


def _write_call(path: str, content: str) -> ToolCall:
    return ToolCall(
        "call_1",
        "write_file",
        json.dumps({"path": path, "content": content}),
    )


def _turn(call: ToolCall) -> ScriptedLLMClient:
    return ScriptedLLMClient(
        [
            [
                StreamEvent(
                    type=EventType.MESSAGE_COMPLETE,
                    tool_calls=(call,),
                )
            ],
            [
                StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta("Done")),
                StreamEvent(type=EventType.MESSAGE_COMPLETE),
            ],
        ]
    )


class MutationApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def _agent(self, client, registry: ToolRegistry, handler) -> Agent:
        return Agent(
            llm_client=client,
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=100,
                token_counter=FixedTokenCounter(),
            ),
            tool_registry=registry,
            approval_handler=handler,
            project_root=self.workspace,
        )

    def _write_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(WriteFileTool(self.workspace))
        return registry

    async def test_a_write_approval_carries_a_creation_diff(self):
        handler = RecordingApprovalHandler(ApprovalDecision.REJECTED)
        client = _turn(_write_call("new.py", "one\ntwo\n"))
        agent = self._agent(client, self._write_registry(), handler)

        [event async for event in agent.run("write it")]

        self.assertEqual(len(handler.requests), 1)
        mutation = handler.requests[0].mutation
        assert isinstance(mutation, FileDiff)
        self.assertEqual(mutation.kind, "create")
        self.assertEqual((mutation.added, mutation.removed), (2, 0))

    async def test_a_write_approval_over_an_existing_file_carries_a_replacement(self):
        (self.workspace / "a.py").write_bytes(b"one\ntwo\n")
        handler = RecordingApprovalHandler(ApprovalDecision.REJECTED)
        client = _turn(_write_call("a.py", "one\nTWO\n"))
        agent = self._agent(client, self._write_registry(), handler)

        [event async for event in agent.run("edit it")]

        mutation = handler.requests[0].mutation
        assert isinstance(mutation, FileDiff)
        self.assertEqual(mutation.kind, "replace")
        self.assertEqual((mutation.added, mutation.removed), (1, 1))

    async def test_an_edit_approval_carries_an_edit_diff(self):
        (self.workspace / "a.py").write_bytes(b"one\ntwo\n")
        handler = RecordingApprovalHandler(ApprovalDecision.REJECTED)
        call = ToolCall(
            "call_1",
            "edit_file",
            json.dumps(
                {
                    "path": "a.py",
                    "edits": [{"old_text": "two", "new_text": "TWO"}],
                }
            ),
        )
        registry = ToolRegistry()
        registry.register(EditFileTool(self.workspace))
        agent = self._agent(_turn(call), registry, handler)

        [event async for event in agent.run("edit it")]

        mutation = handler.requests[0].mutation
        assert isinstance(mutation, FileDiff)
        self.assertEqual(mutation.kind, "edit")

    async def test_a_rejected_preview_still_reaches_approval(self):
        handler = RecordingApprovalHandler(ApprovalDecision.REJECTED)
        client = _turn(_write_call("../escape.py", "content"))
        agent = self._agent(client, self._write_registry(), handler)

        [event async for event in agent.run("write it")]

        self.assertEqual(len(handler.requests), 1)
        self.assertIsNone(handler.requests[0].mutation)

    async def test_a_failing_preview_never_blocks_approval(self):
        handler = RecordingApprovalHandler(ApprovalDecision.REJECTED)
        registry = self._write_registry()

        async def explode(arguments):
            del arguments
            raise RuntimeError("preview is broken")

        registry.get("write_file").preview_mutation = explode  # type: ignore[method-assign]
        agent = self._agent(_turn(_write_call("new.py", "x")), registry, handler)

        [event async for event in agent.run("write it")]

        self.assertEqual(len(handler.requests), 1)
        self.assertIsNone(handler.requests[0].mutation)

    async def test_a_tool_without_a_preview_supplies_no_diff(self):
        from truecoder.tools.builtin import ReadFileTool

        (self.workspace / "a.py").write_bytes(b"one\n")
        handler = RecordingApprovalHandler(ApprovalDecision.REJECTED)
        registry = ToolRegistry()
        registry.register(ReadFileTool(self.workspace))
        call = ToolCall(
            "call_1",
            "read_file",
            json.dumps({"path": "a.py", "start_line": 1, "line_count": 10}),
        )
        agent = self._agent(_turn(call), registry, handler)

        [event async for event in agent.run("read it")]

        self.assertEqual(len(handler.requests), 1)
        self.assertIsNone(handler.requests[0].mutation)

    async def test_the_diff_does_not_change_the_approval_fingerprint(self):
        (self.workspace / "a.py").write_bytes(b"original\n")
        handler = RecordingApprovalHandler(ApprovalDecision.REJECTED)
        agent = self._agent(
            _turn(_write_call("a.py", "replacement\n")),
            self._write_registry(),
            handler,
        )
        [event async for event in agent.run("write it")]
        with_existing_file = handler.requests[0].fingerprint

        (self.workspace / "a.py").write_bytes(b"something else entirely\n")
        second_handler = RecordingApprovalHandler(ApprovalDecision.REJECTED)
        second_agent = self._agent(
            _turn(_write_call("a.py", "replacement\n")),
            self._write_registry(),
            second_handler,
        )
        [event async for event in second_agent.run("write it")]

        self.assertNotEqual(
            handler.requests[0].mutation,
            second_handler.requests[0].mutation,
        )
        self.assertEqual(with_existing_file, second_handler.requests[0].fingerprint)


if __name__ == "__main__":
    unittest.main()
