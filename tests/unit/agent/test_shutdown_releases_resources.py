"""Every durable handle the agent was given must be released when it closes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.unit.agent.test_agent import FixedTokenCounter, ScriptedLLMClient
from truecoder.agent import Agent, ContextBuilder
from truecoder.memory import MemoryStore
from truecoder.tools.mutation_audit import MutationAudit


class RecordingAudit(MutationAudit):
    def __init__(self, database_path: Path) -> None:
        super().__init__(database_path)
        self.closed = 0

    def close(self) -> None:
        self.closed += 1
        super().close()


class FailingAudit(MutationAudit):
    def close(self) -> None:
        raise RuntimeError("the audit refused to close")


class ShutdownReleaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def _agent(self, **overrides) -> Agent:
        return Agent(
            llm_client=ScriptedLLMClient([]),
            project_root=self.root,
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
            **overrides,
        )

    async def test_the_mutation_audit_is_closed(self):
        audit = RecordingAudit(self.root / "mutations.sqlite3")
        agent = self._agent(mutation_audit=audit)

        await agent.close()

        self.assertEqual(audit.closed, 1)

    async def test_closing_releases_the_database_file(self):
        audit = MutationAudit(self.root / "mutations.sqlite3")
        audit.open()
        agent = self._agent(mutation_audit=audit)
        self.assertTrue((self.root / "mutations.sqlite3").exists())

        await agent.close()

        (self.root / "mutations.sqlite3").unlink()

    async def test_the_memory_store_is_still_closed(self):
        store = MemoryStore(self.root / "memory.sqlite3", "workspace_1")
        store.open()
        agent = self._agent(memory_store=store)

        await agent.close()

        (self.root / "memory.sqlite3").unlink()

    async def test_an_audit_that_refuses_to_close_is_counted_not_raised(self):
        agent = self._agent(
            mutation_audit=FailingAudit(self.root / "mutations.sqlite3")
        )

        await agent.close()

        self.assertEqual(agent.close_failures, 1)

    async def test_a_failing_audit_does_not_stop_the_memory_store_closing(self):
        store = MemoryStore(self.root / "memory.sqlite3", "workspace_1")
        store.open()
        agent = self._agent(
            memory_store=store,
            mutation_audit=FailingAudit(self.root / "mutations.sqlite3"),
        )

        await agent.close()

        (self.root / "memory.sqlite3").unlink()

    async def test_closing_twice_is_safe(self):
        audit = RecordingAudit(self.root / "mutations.sqlite3")
        agent = self._agent(mutation_audit=audit)

        await agent.close()
        await agent.close()

        self.assertEqual(audit.closed, 2)
        self.assertEqual(agent.close_failures, 0)

    async def test_a_non_audit_is_rejected(self):
        with self.assertRaises(TypeError):
            self._agent(mutation_audit=object())


if __name__ == "__main__":
    unittest.main()
