from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from truecoder.execution.cancellation import CancellationSource
from truecoder.execution.models import ExecutionContext
from truecoder.tools import ToolExecutionError
from truecoder.tools.builtin import (
    Edit,
    EditFileArguments,
    EditFileTool,
    WriteFileArguments,
    WriteFileTool,
)
from truecoder.tools.context import ToolInvocationContext
from truecoder.tools.mutation_audit import MutationAudit, digest


def _invocation(project_root: Path) -> ToolInvocationContext:
    return ToolInvocationContext(
        execution=ExecutionContext(
            execution_id="exec_1",
            tool_call_id="call_1",
            session_id="session_1",
            turn_id="turn_1",
            workspace_id="workspace_1",
            project_root=project_root,
            launched_at_utc=datetime(2026, 8, 6, tzinfo=UTC),
        ),
        cancellation_source=CancellationSource(),
    )


class MutationRecordingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._directory.name).resolve()
        self.audit = MutationAudit(self.workspace / ".audit" / "mutations.sqlite3")
        self.invocation = _invocation(self.workspace)
        self.addCleanup(self._directory.cleanup)
        self.addCleanup(self.audit.close)

    def _records(self):
        return self.audit.recent("workspace_1")

    async def test_creating_a_file_is_recorded(self):
        tool = WriteFileTool(self.workspace, self.audit)

        await tool.run(
            WriteFileArguments(path="new.py", content="one\ntwo\n"),
            self.invocation,
        )

        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].kind, "create")
        self.assertEqual(records[0].path, "new.py")
        self.assertIsNone(records[0].before_sha256)
        self.assertEqual(records[0].after_sha256, digest(b"one\ntwo\n"))
        self.assertEqual((records[0].lines_added, records[0].lines_removed), (2, 0))

    async def test_replacing_a_file_records_both_digests(self):
        (self.workspace / "a.py").write_bytes(b"one\ntwo\n")
        tool = WriteFileTool(self.workspace, self.audit)

        await tool.run(
            WriteFileArguments(path="a.py", content="one\nTWO\n"),
            self.invocation,
        )

        record = self._records()[0]
        self.assertEqual(record.kind, "replace")
        self.assertEqual(record.before_sha256, digest(b"one\ntwo\n"))
        self.assertEqual(record.after_sha256, digest(b"one\nTWO\n"))
        self.assertEqual((record.lines_added, record.lines_removed), (1, 1))

    async def test_editing_a_file_is_recorded(self):
        (self.workspace / "a.py").write_bytes(b"one\ntwo\nthree\n")
        tool = EditFileTool(self.workspace, self.audit)

        await tool.run(
            EditFileArguments(
                path="a.py",
                edits=[Edit(old_text="two", new_text="TWO")],
            ),
            self.invocation,
        )

        record = self._records()[0]
        self.assertEqual(record.kind, "edit")
        self.assertEqual(record.tool_name, "edit_file")
        self.assertEqual(record.before_sha256, digest(b"one\ntwo\nthree\n"))
        self.assertEqual(record.after_sha256, digest(b"one\nTWO\nthree\n"))

    async def test_the_record_carries_the_call_identity(self):
        tool = WriteFileTool(self.workspace, self.audit)

        await tool.run(
            WriteFileArguments(path="new.py", content="x\n"),
            self.invocation,
        )

        record = self._records()[0]
        self.assertEqual(record.tool_call_id, "call_1")
        self.assertEqual(record.session_id, "session_1")
        self.assertEqual(record.turn_id, "turn_1")
        self.assertEqual(record.workspace_id, "workspace_1")

    async def test_a_failed_write_records_nothing(self):
        tool = WriteFileTool(self.workspace, self.audit)

        with self.assertRaises(ToolExecutionError):
            await tool.run(
                WriteFileArguments(path="../escape.py", content="x"),
                self.invocation,
            )

        self.assertEqual(self._records(), ())

    async def test_a_failed_edit_records_nothing(self):
        (self.workspace / "a.py").write_bytes(b"one\n")
        tool = EditFileTool(self.workspace, self.audit)

        with self.assertRaises(ToolExecutionError):
            await tool.run(
                EditFileArguments(
                    path="a.py",
                    edits=[
                        Edit(
                            old_text="missing",
                            new_text="x",
                        )
                    ],
                ),
                self.invocation,
            )

        self.assertEqual(self._records(), ())

    async def test_a_tool_without_an_audit_still_writes(self):
        tool = WriteFileTool(self.workspace)

        await tool.run(
            WriteFileArguments(path="new.py", content="x\n"),
            self.invocation,
        )

        self.assertEqual(
            (self.workspace / "new.py").read_text(encoding="utf-8"),
            "x\n",
        )

    async def test_a_call_without_an_invocation_records_nothing(self):
        tool = WriteFileTool(self.workspace, self.audit)

        await tool.run(WriteFileArguments(path="new.py", content="x\n"))

        self.assertEqual(self._records(), ())
        self.assertEqual((self.workspace / "new.py").read_text(encoding="utf-8"), "x\n")

    async def test_an_unrecordable_mutation_still_completes(self):
        broken = MutationAudit(self.workspace / "blocked.sqlite3")
        broken.open()
        broken.close()
        broken.database_path.write_bytes(b"not a database")
        tool = WriteFileTool(self.workspace, broken)

        result = await tool.run(
            WriteFileArguments(path="new.py", content="x\n"),
            self.invocation,
        )

        self.assertEqual(result["bytes_written"], 2)
        self.assertEqual((self.workspace / "new.py").read_text(encoding="utf-8"), "x\n")
        self.assertEqual(broken.failures, 1)

    async def test_a_non_audit_collaborator_is_rejected(self):
        with self.assertRaises(TypeError):
            WriteFileTool(self.workspace, object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            EditFileTool(self.workspace, object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
