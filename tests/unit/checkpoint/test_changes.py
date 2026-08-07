from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from truecoder.checkpoint.changes import (
    FileChange,
    WorkspaceChanges,
    compute_changes,
)
from truecoder.checkpoint.git import GitWorkspace
from truecoder.checkpoint.service import CheckpointService


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def _run(root: Path, *arguments: str) -> None:
    subprocess.run(list(arguments), cwd=root, check=True, capture_output=True)


def _change(path: str, kind, added: int = 0, removed: int = 0) -> FileChange:
    from truecoder.mutation import build_file_diff

    before = "\n".join(f"old {index}" for index in range(removed))
    after = "\n".join(f"new {index}" for index in range(added))
    return FileChange(
        path=path,
        kind=kind,
        diff=build_file_diff(path, before, after, kind="replace"),
    )


class WorkspaceChangesModelTests(unittest.TestCase):
    def test_an_empty_change_set_says_so(self):
        changes = WorkspaceChanges(label="turn", changes=())

        self.assertTrue(changes.is_empty)
        self.assertEqual(changes.summary, "No files changed")

    def test_line_totals_are_summed(self):
        changes = WorkspaceChanges(
            label="turn",
            changes=(_change("a.py", "modified", 3, 1), _change("b.py", "modified", 2, 2)),
        )

        self.assertEqual(changes.added, 5)
        self.assertEqual(changes.removed, 3)
        self.assertIn("2 file(s) changed", changes.summary)
        self.assertIn("+5", changes.summary)

    def test_the_total_counts_changes_beyond_those_shown(self):
        changes = WorkspaceChanges(
            label="turn",
            changes=(_change("a.py", "modified", 1, 0),),
            total=12,
        )

        self.assertTrue(changes.truncated)
        self.assertIn("12 file(s) changed", changes.summary)
        self.assertIn("showing 1", changes.summary)

    def test_an_untruncated_set_is_not_marked_truncated(self):
        changes = WorkspaceChanges(
            label="turn",
            changes=(_change("a.py", "modified", 1, 0),),
        )

        self.assertFalse(changes.truncated)
        self.assertIn("1 file(s) changed", changes.summary)

    def test_a_change_without_a_diff_describes_itself(self):
        change = FileChange(path="logo.png", kind="modified", diff=None)

        self.assertEqual(change.added, 0)
        self.assertIn("not shown as text", change.summary)


class ComputeChangesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        _git(self.root, "init", "-q", ".")
        _git(self.root, "config", "user.email", "t@t")
        _git(self.root, "config", "user.name", "t")
        (self.root / "app.py").write_bytes(b"def parse(raw):\n    return raw\n")
        (self.root / "util.py").write_bytes(b"x = 1\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "init")
        self.workspace = GitWorkspace(self.root)
        self.service = CheckpointService(self.workspace)
        self.checkpoint = await self.service.capture("before the turn")
        assert self.checkpoint is not None
        self.addCleanup(self._directory.cleanup)

    async def _changes(self, **kwargs) -> WorkspaceChanges:
        return await compute_changes(self.workspace, self.checkpoint, **kwargs)

    async def test_an_untouched_workspace_reports_nothing(self):
        self.assertTrue((await self._changes()).is_empty)

    async def test_a_modified_file_is_reported_with_its_diff(self):
        (self.root / "app.py").write_bytes(b"def parse(raw):\n    return int(raw)\n")

        changes = await self._changes()

        self.assertEqual(len(changes.changes), 1)
        change = changes.changes[0]
        self.assertEqual(change.path, "app.py")
        self.assertEqual(change.kind, "modified")
        assert change.diff is not None
        self.assertEqual((change.added, change.removed), (1, 1))

    async def test_a_change_made_outside_the_edit_tools_is_reported(self):
        _run(self.root, "sed", "-i", "s/x = 1/x = 999/", "util.py")

        changes = await self._changes()

        self.assertEqual([c.path for c in changes.changes], ["util.py"])
        assert changes.changes[0].diff is not None
        self.assertEqual(changes.changes[0].diff.added, 1)

    async def test_a_staged_new_file_is_reported_as_added(self):
        (self.root / "created.py").write_bytes(b"one\ntwo\n")

        changes = await self._changes()

        change = next(c for c in changes.changes if c.path == "created.py")
        self.assertEqual(change.kind, "added")
        assert change.diff is not None
        self.assertEqual((change.added, change.removed), (2, 0))

    async def test_a_deleted_file_is_reported_as_deleted(self):
        (self.root / "util.py").unlink()

        changes = await self._changes()

        change = next(c for c in changes.changes if c.path == "util.py")
        self.assertEqual(change.kind, "deleted")
        assert change.diff is not None
        self.assertEqual((change.added, change.removed), (0, 1))

    async def test_a_new_unstaged_file_is_reported_with_its_content(self):
        (self.root / "scratch.txt").write_bytes(b"notes\nmore\n")

        changes = await self._changes()

        change = next(c for c in changes.changes if c.path == "scratch.txt")
        self.assertEqual(change.kind, "added")
        assert change.diff is not None
        self.assertEqual((change.added, change.removed), (2, 0))

    async def test_a_file_untracked_before_the_turn_is_not_reported(self):
        (self.root / "notes.md").write_bytes(b"my scratch notes\n")
        checkpoint = await self.service.capture("with notes")
        assert checkpoint is not None
        (self.root / "app.py").write_bytes(b"changed\n")

        changes = await compute_changes(self.workspace, checkpoint)

        self.assertEqual([c.path for c in changes.changes], ["app.py"])

    async def test_ignored_files_are_not_reported(self):
        (self.root / ".gitignore").write_bytes(b"secret.txt\n")
        _git(self.root, "add", ".gitignore")
        _git(self.root, "commit", "-qm", "ignore")
        checkpoint = await self.service.capture("with ignore")
        assert checkpoint is not None
        (self.root / "secret.txt").write_bytes(b"private\n")

        changes = await compute_changes(self.workspace, checkpoint)

        self.assertTrue(changes.is_empty)

    async def test_a_binary_file_is_listed_without_a_diff(self):
        (self.root / "blob.bin").write_bytes(b"\xff\xfe\x00\x01binary")

        changes = await self._changes()

        change = next(c for c in changes.changes if c.path == "blob.bin")
        self.assertIsNone(change.diff)

    async def test_the_file_count_is_bounded(self):
        for index in range(8):
            (self.root / f"file{index}.py").write_bytes(b"content\n")

        changes = await compute_changes(self.workspace, self.checkpoint, max_files=3)

        self.assertEqual(len(changes.changes), 3)
        self.assertTrue(changes.truncated)
        self.assertEqual(changes.total, 8)

    async def test_an_invalid_bound_is_rejected(self):
        with self.assertRaises(ValueError):
            await self._changes(max_files=0)

    async def test_the_service_reports_changes_since_the_latest_checkpoint(self):
        (self.root / "app.py").write_bytes(b"changed\n")

        changes = await self.service.latest_changes()

        assert changes is not None
        self.assertEqual([c.path for c in changes.changes], ["app.py"])

    async def test_the_service_reports_nothing_without_a_checkpoint(self):
        service = CheckpointService(GitWorkspace(self.root))
        for entry in await service.list():
            await self.workspace.delete_ref(entry.ref)

        self.assertIsNone(await service.latest_changes())


if __name__ == "__main__":
    unittest.main()
