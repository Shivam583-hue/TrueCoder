from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.unit.agent.test_agent import FixedTokenCounter, ScriptedLLMClient
from tests.unit.context.test_context import LeafLengthTokenCounter, state_with_turns
from truecoder.agent import Agent, AgentEventType, ContextBuilder
from truecoder.agent.prompts import MEMORY_TOOL_GUIDANCE, build_system_prompt
from truecoder.client.response import EventType, StreamEvent, TextDelta
from truecoder.memory import MemoryStore
from truecoder.tools import ToolApproval, ToolCall, ToolRegistry, ToolResultStatus
from truecoder.tools.builtin import ForgetTool, RememberTool, memory_tools
from truecoder.tools.executor import ToolExecutor


def _reply(text: str = "done") -> list[StreamEvent]:
    return [
        StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta(text)),
        StreamEvent(type=EventType.MESSAGE_COMPLETE),
    ]


class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.store = MemoryStore(
            Path(self._directory.name) / "memory.sqlite3",
            "workspace_1",
        )
        self.addCleanup(self._directory.cleanup)
        self.addCleanup(self.store.close)

    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        for tool in memory_tools(self.store):
            registry.register(tool)
        return registry

    async def _call(self, name: str, arguments: dict):
        registry = self._registry()
        call = ToolCall("call_1", name, json.dumps(arguments))
        return await ToolExecutor(registry).execute(call, approved=True)

    def test_both_tools_require_approval(self):
        for tool in memory_tools(self.store):
            with self.subTest(tool=tool.name):
                self.assertIs(tool.approval, ToolApproval.REQUIRED)

    def test_a_store_is_required(self):
        with self.assertRaises(TypeError):
            RememberTool(object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ForgetTool(object())  # type: ignore[arg-type]

    async def test_remembering_stores_the_note(self):
        result = await self._call("remember", {"note": "tests live in tests/"})

        self.assertIs(result.status, ToolResultStatus.SUCCESS)
        self.assertEqual(result.output["stored"], 1)
        self.assertEqual([e.note for e in self.store.entries()], ["tests live in tests/"])

    async def test_remembering_twice_stores_one_note(self):
        await self._call("remember", {"note": "one fact"})
        result = await self._call("remember", {"note": "one fact"})

        self.assertEqual(result.output["stored"], 1)

    async def test_forgetting_removes_the_note(self):
        await self._call("remember", {"note": "temporary"})

        result = await self._call("forget", {"note": "temporary"})

        self.assertTrue(result.output["removed"])
        self.assertEqual(self.store.entries(), ())

    async def test_forgetting_an_absent_note_is_not_an_error(self):
        result = await self._call("forget", {"note": "never recorded"})

        self.assertIs(result.status, ToolResultStatus.SUCCESS)
        self.assertFalse(result.output["removed"])

    async def test_an_unusable_store_is_a_recoverable_failure(self):
        self.store.close()
        self.store.database_path.write_bytes(b"not a database")

        result = await self._call("remember", {"note": "anything"})

        self.assertIs(result.status, ToolResultStatus.ERROR)
        self.assertEqual(result.error_code, "memory_unavailable")

    def test_an_empty_note_is_rejected_during_parsing(self):
        from truecoder.tools.base import ToolArgumentError

        with self.assertRaises(ToolArgumentError):
            RememberTool(self.store).parse_arguments(json.dumps({"note": ""}))

    def test_unknown_fields_are_rejected_during_parsing(self):
        from truecoder.tools.base import ToolArgumentError

        with self.assertRaises(ToolArgumentError):
            RememberTool(self.store).parse_arguments(
                json.dumps({"note": "x", "scope": "global"})
            )


class MemoryContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.store = MemoryStore(
            Path(self._directory.name) / "memory.sqlite3",
            "workspace_1",
        )
        self.addCleanup(self._directory.cleanup)
        self.addCleanup(self.store.close)

    def _builder(self, store: MemoryStore | None) -> ContextBuilder:
        return ContextBuilder(
            system_prompt="S",
            max_input_tokens=100_000,
            token_counter=LeafLengthTokenCounter(),
            memory_store=store,
        )

    def test_no_memory_message_without_a_store(self):
        self.assertIsNone(self._builder(None).memory_message())

    def test_no_memory_message_while_empty(self):
        self.assertIsNone(self._builder(self.store).memory_message())

    def test_notes_reach_the_request(self):
        self.store.remember("the parser lives in src/parse.py")
        builder = self._builder(self.store)

        messages = builder.build(state_with_turns([], "Q1"))

        self.assertEqual(messages[0]["content"], "S")
        self.assertIn("src/parse.py", messages[1]["content"])

    def test_memory_sits_before_the_conversation(self):
        self.store.remember("a durable fact")
        builder = self._builder(self.store)

        messages = builder.build(state_with_turns([("Q1", "A1")], "Q2"))

        self.assertIn("a durable fact", messages[1]["content"])
        self.assertEqual(messages[-1]["content"], "Q2")

    def test_a_forgotten_note_leaves_the_request(self):
        self.store.remember("temporary")
        builder = self._builder(self.store)
        self.store.forget_note("temporary")

        messages = builder.build(state_with_turns([], "Q1"))

        self.assertNotIn(
            "temporary",
            " ".join(str(m["content"]) for m in messages),
        )

    def test_an_unreadable_store_never_blocks_a_request(self):
        self.store.remember("one")
        self.store.close()
        self.store.database_path.write_bytes(b"not a database")
        builder = self._builder(self.store)

        messages = builder.build(state_with_turns([], "Q1"))

        self.assertEqual([m["role"] for m in messages], ["system", "user"])

    def test_a_non_store_is_rejected(self):
        with self.assertRaises(TypeError):
            self._builder(object())  # type: ignore[arg-type]


class MemoryAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.store = MemoryStore(
            Path(self._directory.name) / "memory.sqlite3",
            "workspace_1",
        )
        self.addCleanup(self._directory.cleanup)
        self.addCleanup(self.store.close)

    def _agent(self, store: MemoryStore | None) -> Agent:
        return Agent(
            llm_client=ScriptedLLMClient([_reply()]),
            memory_store=store,
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
        )

    def test_a_store_registers_both_tools_and_guidance(self):
        agent = self._agent(self.store)

        self.assertIn("remember", agent.tool_registry)
        self.assertIn("forget", agent.tool_registry)
        self.assertIn(
            MEMORY_TOOL_GUIDANCE.strip(),
            agent.context_builder.system_prompt,
        )

    def test_no_store_means_no_memory_tools(self):
        agent = self._agent(None)

        self.assertNotIn("remember", agent.tool_registry)
        self.assertIsNone(agent.context_builder.memory_store)

    def test_the_registered_tools_share_the_agent_store(self):
        agent = self._agent(self.store)

        self.assertIs(agent.tool_registry.get("remember").store, self.store)
        self.assertIs(agent.tool_registry.get("forget").store, self.store)

    def test_a_non_store_is_rejected(self):
        with self.assertRaises(TypeError):
            self._agent(object())  # type: ignore[arg-type]

    async def test_a_recorded_note_reaches_the_next_request(self):
        self.store.remember("the parser lives in src/parse.py")
        client = ScriptedLLMClient([_reply()])
        agent = Agent(
            llm_client=client,
            memory_store=self.store,
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
        )

        [event async for event in agent.run("where is the parser")]

        contents = [m["content"] for m in client.calls[0]["messages"]]
        self.assertTrue(any("src/parse.py" in (c or "") for c in contents))

    async def test_guidance_is_absent_without_memory(self):
        agent = self._agent(None)

        events = [event async for event in agent.run("hello")]

        self.assertTrue(any(e.type is AgentEventType.AGENT_END for e in events))
        self.assertNotIn(
            MEMORY_TOOL_GUIDANCE.strip(),
            agent.context_builder.system_prompt,
        )


class MemoryGuidanceTests(unittest.TestCase):
    def test_guidance_warns_against_secrets_and_duplication(self):
        self.assertIn("Never record\nsecrets", MEMORY_TOOL_GUIDANCE)
        self.assertIn("AGENTS.md", MEMORY_TOOL_GUIDANCE)

    def test_guidance_is_not_duplicated(self):
        from truecoder.agent.prompts import add_memory_tool_guidance

        prompt = add_memory_tool_guidance(build_system_prompt())

        self.assertEqual(add_memory_tool_guidance(prompt), prompt)


if __name__ == "__main__":
    unittest.main()
