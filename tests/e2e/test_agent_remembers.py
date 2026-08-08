"""What the agent records must be what it is told back on the next turn."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers.turns import ScriptedModel, TokenCounter, calls, says
from truecoder.agent import (
    Agent,
    ApprovalResponse,
    ApprovalScope,
    ContextBuilder,
)
from truecoder.memory import MemoryStore
from truecoder.tools import ToolRegistry

BUDGET = 64000


class MemoryTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)
        self.store = MemoryStore(self.root / "memory.sqlite3", "workspace_1")

    def _agent(self, model: ScriptedModel) -> Agent:
        agent = Agent(
            llm_client=model,
            tool_registry=ToolRegistry(),
            project_root=self.root,
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=BUDGET,
                token_counter=TokenCounter(),
            ),
            memory_store=self.store,
        )

        async def approve(request):
            del request
            return ApprovalResponse.approve(ApprovalScope.ONCE)

        agent.approval_handler = approve
        self.addAsyncCleanup(agent.close)
        return agent

    async def _run(self, agent: Agent, prompt: str) -> None:
        [event async for event in agent.run(prompt)]

    def _notes(self) -> list[str]:
        return [entry.note for entry in self.store.entries()]

    def _memory_seen_by(self, model: ScriptedModel) -> str:
        for request in reversed(model.requests):
            for message in request:
                content = message.get("content")
                if isinstance(content, str) and "Durable notes" in content:
                    return content
        return ""

    async def test_a_recorded_note_is_told_back_on_the_next_turn(self):
        first = ScriptedModel(
            [
                calls(("remember", {"note": "Tests run with unittest"})),
                says("Noted."),
            ]
        )
        await self._run(self._agent(first), "remember how tests run")

        second = ScriptedModel([says("I know that already.")])
        await self._run(self._agent(second), "how do tests run?")

        self.assertIn("Tests run with unittest", self._memory_seen_by(second))

    async def test_a_correction_leaves_one_note_the_model_can_read(self):
        first = ScriptedModel(
            [
                calls(("remember", {"note": "The parser lives in src/parse.py"})),
                says("Noted."),
            ]
        )
        await self._run(self._agent(first), "remember where the parser is")

        second = ScriptedModel(
            [
                calls(
                    (
                        "remember",
                        {
                            "note": "The parser lives in src/parser/core.py",
                            "replaces": "The parser lives in src/parse.py",
                        },
                    )
                ),
                says("Corrected."),
            ]
        )
        await self._run(self._agent(second), "actually it moved")

        third = ScriptedModel([says("Understood.")])
        await self._run(self._agent(third), "where is the parser?")

        seen = self._memory_seen_by(third)
        self.assertIn("src/parser/core.py", seen)
        self.assertNotIn("src/parse.py\n", seen)
        self.assertEqual(self._notes(), ["The parser lives in src/parser/core.py"])

    async def test_a_forgotten_note_stops_being_told_back(self):
        first = ScriptedModel(
            [calls(("remember", {"note": "Use tabs"})), says("Noted.")]
        )
        await self._run(self._agent(first), "remember the style")

        second = ScriptedModel(
            [calls(("forget", {"note": "use tabs."})), says("Dropped.")]
        )
        await self._run(self._agent(second), "forget that")

        third = ScriptedModel([says("Nothing recorded.")])
        await self._run(self._agent(third), "what do you know?")

        self.assertEqual(self._notes(), [])
        self.assertEqual(self._memory_seen_by(third), "")

    async def test_a_rejected_note_is_never_recorded(self):
        model = ScriptedModel(
            [calls(("remember", {"note": "Should not persist"})), says("Fine.")]
        )
        agent = self._agent(model)

        async def reject(request):
            del request
            return ApprovalResponse.reject()

        agent.approval_handler = reject

        await self._run(agent, "remember something")

        self.assertEqual(self._notes(), [])


if __name__ == "__main__":
    unittest.main()
