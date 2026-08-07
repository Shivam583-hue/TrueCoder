from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from truecoder.checkpoint.git import GitUnavailableError, GitWorkspace
from truecoder.checkpoint.models import (
    Checkpoint,
    decode_message,
    encode_message,
    normalize_label,
)
from truecoder.checkpoint.service import CheckpointService


def _status(root: Path) -> str:
    completed = subprocess.run(
        ["git", "status", "--short"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    )


class MessageTests(unittest.TestCase):
    def test_metadata_round_trips(self):
        message = encode_message(
            label="before turn",
            checkpoint_id="abc",
            created_at="2026-08-07T00:00:00+00:00",
            session_id="s1",
            turn_id="t1",
        )

        decoded = decode_message(message)

        self.assertEqual(decoded["id"], "abc")
        self.assertEqual(decoded["session_id"], "s1")
        self.assertEqual(decoded["label"], "before turn")

    def test_the_first_line_is_human_readable(self):
        message = encode_message(
            label="before turn",
            checkpoint_id="abc",
            created_at="x",
            session_id="",
            turn_id="",
        )

        self.assertTrue(message.startswith("TrueCoder checkpoint: before turn"))

    def test_a_message_without_metadata_decodes_empty(self):
        self.assertEqual(decode_message("just a commit"), {})

    def test_corrupt_metadata_decodes_empty(self):
        self.assertEqual(decode_message("truecoder-checkpoint: {not json"), {})

    def test_labels_are_normalized(self):
        self.assertEqual(normalize_label("  many   spaces  "), "many spaces")
        self.assertEqual(normalize_label("   "), "untitled")
        self.assertEqual(len(normalize_label("x" * 500)), 120)

    def test_a_checkpoint_requires_identity(self):
        with self.assertRaises(ValueError):
            Checkpoint(checkpoint_id=" ", commit="c", tree="t", label="l", created_at="")

    def test_a_checkpoint_names_its_ref(self):
        checkpoint = Checkpoint(
            checkpoint_id="abc",
            commit="c",
            tree="t",
            label="l",
            created_at="",
        )

        self.assertEqual(checkpoint.ref, "refs/truecoder/checkpoints/abc")


class GitWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def _repository(self) -> GitWorkspace:
        _git(self.root, "init", "-q", ".")
        _git(self.root, "config", "user.email", "t@t")
        _git(self.root, "config", "user.name", "t")
        (self.root / "app.py").write_bytes(b"original\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "init")
        return GitWorkspace(self.root)

    async def test_a_plain_directory_is_refused(self):
        workspace = GitWorkspace(self.root)

        reason = await workspace.unavailable_reason()

        assert reason is not None
        self.assertIn("not a git repository", reason)

    async def test_a_missing_git_is_refused(self):
        workspace = GitWorkspace(self.root, executable=None)
        workspace._executable = None

        reason = await workspace.unavailable_reason()

        assert reason is not None
        self.assertIn("git is not installed", reason)

    async def test_require_raises_when_unavailable(self):
        with self.assertRaises(GitUnavailableError) as caught:
            await GitWorkspace(self.root).require()

        self.assertEqual(caught.exception.code, "checkpoints_unavailable")

    async def test_a_repository_is_available(self):
        self.assertTrue(await self._repository().available())

    async def test_a_snapshot_does_not_touch_the_user_index(self):
        workspace = self._repository()
        (self.root / "staged.txt").write_bytes(b"work\n")
        _git(self.root, "add", "staged.txt")

        await workspace.snapshot_tree()

        self.assertIn("A  staged.txt", _status(self.root))

    async def test_a_snapshot_captures_uncommitted_work(self):
        workspace = self._repository()
        (self.root / "app.py").write_bytes(b"changed\n")

        tree = await workspace.snapshot_tree()

        self.assertIn("app.py", await workspace.paths_in_tree(tree))

    async def test_restoring_a_tree_returns_the_content(self):
        workspace = self._repository()
        tree = await workspace.snapshot_tree()
        (self.root / "app.py").write_bytes(b"changed\n")

        await workspace.restore_tree(tree)

        self.assertEqual((self.root / "app.py").read_bytes(), b"original\n")


class CheckpointServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        _git(self.root, "init", "-q", ".")
        _git(self.root, "config", "user.email", "t@t")
        _git(self.root, "config", "user.name", "t")
        (self.root / "app.py").write_bytes(b"original\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "init")
        self.service = CheckpointService(GitWorkspace(self.root))
        self.addCleanup(self._directory.cleanup)

    async def test_capturing_records_a_checkpoint(self):
        checkpoint = await self.service.capture("before turn one")

        assert checkpoint is not None
        self.assertEqual(checkpoint.label, "before turn one")
        self.assertEqual(len(await self.service.list()), 1)

    async def test_an_unchanged_workspace_is_not_captured_twice(self):
        await self.service.capture("first")

        self.assertIsNone(await self.service.capture("second"))
        self.assertEqual(len(await self.service.list()), 1)

    async def test_a_changed_workspace_is_captured_again(self):
        await self.service.capture("first")
        (self.root / "app.py").write_bytes(b"changed\n")

        second = await self.service.capture("second")

        assert second is not None
        self.assertEqual(len(await self.service.list()), 2)

    async def test_checkpoints_are_listed_newest_first(self):
        await self.service.capture("first")
        (self.root / "app.py").write_bytes(b"changed\n")
        await self.service.capture("second")

        labels = [entry.label for entry in await self.service.list()]

        self.assertEqual(labels, ["second", "first"])

    async def test_restoring_returns_the_earlier_content(self):
        checkpoint = await self.service.capture("before")
        assert checkpoint is not None
        (self.root / "app.py").write_bytes(b"agent changed this\n")

        await self.service.restore(checkpoint.checkpoint_id)

        self.assertEqual((self.root / "app.py").read_bytes(), b"original\n")

    async def test_restoring_captures_a_safety_checkpoint_first(self):
        checkpoint = await self.service.capture("before")
        assert checkpoint is not None
        (self.root / "app.py").write_bytes(b"agent changed this\n")

        outcome = await self.service.restore(checkpoint.checkpoint_id)

        assert outcome.safety is not None
        self.assertEqual(outcome.safety.label, "before restore")

    async def test_the_safety_checkpoint_can_undo_the_restore(self):
        checkpoint = await self.service.capture("before")
        assert checkpoint is not None
        (self.root / "app.py").write_bytes(b"agent changed this\n")
        outcome = await self.service.restore(checkpoint.checkpoint_id)
        assert outcome.safety is not None

        await self.service.restore(outcome.safety.checkpoint_id)

        self.assertEqual((self.root / "app.py").read_bytes(), b"agent changed this\n")

    async def test_a_restore_reports_files_it_removed(self):
        checkpoint = await self.service.capture("before")
        assert checkpoint is not None
        (self.root / "added.py").write_bytes(b"new\n")
        _git(self.root, "add", "added.py")

        outcome = await self.service.restore(checkpoint.checkpoint_id)

        self.assertEqual(outcome.removed, ("added.py",))
        self.assertFalse((self.root / "added.py").exists())

    async def test_a_preview_names_what_a_restore_would_remove(self):
        checkpoint = await self.service.capture("before")
        assert checkpoint is not None
        (self.root / "added.py").write_bytes(b"new\n")
        _git(self.root, "add", "added.py")

        self.assertEqual(await self.service.preview(checkpoint), ("added.py",))

    async def test_untracked_files_survive_a_restore(self):
        checkpoint = await self.service.capture("before")
        assert checkpoint is not None
        (self.root / "scratch.txt").write_bytes(b"mine\n")

        await self.service.restore(checkpoint.checkpoint_id)

        self.assertTrue((self.root / "scratch.txt").exists())

    async def test_an_unknown_checkpoint_is_refused(self):
        with self.assertRaises(GitUnavailableError) as caught:
            await self.service.restore("nope")

        self.assertEqual(caught.exception.code, "checkpoint_not_found")

    async def test_old_checkpoints_are_pruned(self):
        service = CheckpointService(GitWorkspace(self.root), limit=3)
        for index in range(5):
            (self.root / "app.py").write_bytes(f"version {index}\n".encode())
            await service.capture(f"turn {index}")

        self.assertEqual(len(await service.list()), 3)

    async def test_pruning_keeps_the_newest(self):
        service = CheckpointService(GitWorkspace(self.root), limit=2)
        for index in range(4):
            (self.root / "app.py").write_bytes(f"version {index}\n".encode())
            await service.capture(f"turn {index}")

        labels = [entry.label for entry in await service.list()]

        self.assertEqual(labels, ["turn 3", "turn 2"])

    async def test_metadata_survives_a_round_trip(self):
        checkpoint = await self.service.capture(
            "before turn",
            session_id="session-1",
            turn_id="turn-1",
        )

        assert checkpoint is not None
        stored = await self.service.find(checkpoint.checkpoint_id)
        assert stored is not None
        self.assertEqual(stored.session_id, "session-1")
        self.assertEqual(stored.turn_id, "turn-1")

    async def test_a_non_workspace_is_rejected(self):
        with self.assertRaises(TypeError):
            CheckpointService(object())  # type: ignore[arg-type]

    async def test_an_invalid_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            CheckpointService(GitWorkspace(self.root), limit=0)


if __name__ == "__main__":
    unittest.main()
