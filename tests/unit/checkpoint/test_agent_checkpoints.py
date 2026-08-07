from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.unit.agent.test_agent import FixedTokenCounter, ScriptedLLMClient
from truecoder.agent import Agent, ContextBuilder
from truecoder.checkpoint import CheckpointService, GitWorkspace
from truecoder.client.response import EventType, StreamEvent, TextDelta


def _reply(text: str = "done") -> list[StreamEvent]:
    return [
        StreamEvent(type=EventType.TEXT_DELTA, text_delta=TextDelta(text)),
        StreamEvent(type=EventType.MESSAGE_COMPLETE),
    ]


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


class AgentCheckpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def _initialise(self) -> None:
        _git(self.root, "init", "-q", ".")
        _git(self.root, "config", "user.email", "t@t")
        _git(self.root, "config", "user.name", "t")
        (self.root / "app.py").write_bytes(b"original\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "init")

    def _agent(self, service: CheckpointService | None, replies: int = 1) -> Agent:
        return Agent(
            llm_client=ScriptedLLMClient([_reply() for _ in range(replies)]),
            project_root=self.root,
            checkpoints=service,
            context_builder=ContextBuilder(
                system_prompt="test system",
                max_input_tokens=1000,
                token_counter=FixedTokenCounter(),
            ),
        )

    async def test_a_turn_captures_a_checkpoint_first(self):
        self._initialise()
        service = CheckpointService(GitWorkspace(self.root))
        agent = self._agent(service)

        [event async for event in agent.run("fix the parser")]

        checkpoints = await service.list()
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0].label, "fix the parser")

    async def test_the_checkpoint_holds_the_state_before_the_turn(self):
        self._initialise()
        service = CheckpointService(GitWorkspace(self.root))
        agent = self._agent(service)

        [event async for event in agent.run("change it")]
        (self.root / "app.py").write_bytes(b"the agent changed this\n")
        checkpoints = await service.list()
        await service.restore(checkpoints[0].checkpoint_id)

        self.assertEqual((self.root / "app.py").read_bytes(), b"original\n")

    async def test_the_checkpoint_records_the_session(self):
        self._initialise()
        service = CheckpointService(GitWorkspace(self.root))
        agent = self._agent(service)

        [event async for event in agent.run("go")]

        self.assertTrue((await service.list())[0].session_id)

    async def test_an_unchanged_workspace_is_not_captured_again(self):
        self._initialise()
        service = CheckpointService(GitWorkspace(self.root))
        agent = self._agent(service, replies=2)

        [event async for event in agent.run("first")]
        [event async for event in agent.run("second")]

        self.assertEqual(len(await service.list()), 1)

    async def test_a_non_repository_never_blocks_a_turn(self):
        service = CheckpointService(GitWorkspace(self.root))
        agent = self._agent(service)

        events = [event async for event in agent.run("go")]

        self.assertTrue(events)
        self.assertEqual(agent.checkpoint_failures, 1)
        self.assertEqual(events[-1].data.get("response"), "done")

    async def test_no_service_means_no_checkpoints(self):
        self._initialise()
        agent = self._agent(None)

        events = [event async for event in agent.run("go")]

        self.assertEqual(agent.checkpoint_failures, 0)
        self.assertEqual(events[-1].data.get("response"), "done")

    async def test_a_non_service_is_rejected(self):
        with self.assertRaises(TypeError):
            self._agent(object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
