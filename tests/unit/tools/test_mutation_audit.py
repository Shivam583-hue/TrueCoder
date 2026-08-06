from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from truecoder.execution.errors import AuditUnavailableError
from truecoder.tools.mutation_audit import (
    MUTATION_SCHEMA_VERSION,
    MutationAudit,
    MutationRecord,
    digest,
)


class FrozenClock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)

    def now_utc(self) -> datetime:
        return self.moment

    def monotonic(self) -> float:
        return 0.0

    async def sleep(self, seconds: float) -> None:
        del seconds


class MutationAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._directory.name) / "mutations.sqlite3"
        self.audit = MutationAudit(self.database_path, clock=FrozenClock())
        self.addCleanup(self._directory.cleanup)
        self.addCleanup(self.audit.close)

    def _record(self, **overrides):
        payload = {
            "tool_call_id": "call_1",
            "session_id": "session_1",
            "turn_id": "turn_1",
            "workspace_id": "workspace_1",
            "tool_name": "write_file",
            "path": "src/app.py",
            "kind": "replace",
            "before": b"one\n",
            "after": b"two\n",
            "lines_added": 1,
            "lines_removed": 1,
        }
        payload.update(overrides)
        return self.audit.record(**payload)  # type: ignore[arg-type]

    def test_the_schema_is_installed_on_first_use(self):
        self.audit.open()

        connection = sqlite3.connect(self.database_path)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(version, MUTATION_SCHEMA_VERSION)

    def test_a_recorded_mutation_is_readable(self):
        written = self._record()

        assert written is not None
        stored = self.audit.recent("workspace_1")

        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0], written)

    def test_digests_cover_both_sides_of_the_change(self):
        record = self._record(before=b"one\n", after=b"two\n")

        assert record is not None
        self.assertEqual(record.before_sha256, digest(b"one\n"))
        self.assertEqual(record.after_sha256, digest(b"two\n"))
        self.assertEqual((record.before_bytes, record.after_bytes), (4, 4))

    def test_a_created_file_records_no_prior_digest(self):
        record = self._record(kind="create", before=None, lines_removed=0)

        assert record is not None
        self.assertIsNone(record.before_sha256)
        self.assertEqual(record.before_bytes, 0)

    def test_records_are_scoped_by_workspace(self):
        self._record(workspace_id="workspace_1")
        self._record(workspace_id="workspace_2")

        self.assertEqual(len(self.audit.recent("workspace_1")), 1)
        self.assertEqual(len(self.audit.recent("workspace_2")), 1)

    def test_recent_returns_the_newest_first(self):
        first = self._record(path="first.py")
        second = self._record(path="second.py")

        assert first is not None and second is not None
        paths = [record.path for record in self.audit.recent("workspace_1")]

        self.assertEqual(paths, ["second.py", "first.py"])

    def test_recent_honours_its_limit(self):
        for index in range(5):
            self._record(path=f"file{index}.py")

        self.assertEqual(len(self.audit.recent("workspace_1", limit=2)), 2)

    def test_an_invalid_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.audit.recent("workspace_1", limit=0)

    def test_records_cannot_be_updated(self):
        record = self._record()
        assert record is not None
        self.audit.open()

        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE mutation_records SET path = 'other.py' WHERE record_id = ?",
                    (record.record_id,),
                )
        finally:
            connection.close()

    def test_records_cannot_be_deleted(self):
        record = self._record()
        assert record is not None
        self.audit.open()

        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "DELETE FROM mutation_records WHERE record_id = ?",
                    (record.record_id,),
                )
        finally:
            connection.close()

    def test_a_storage_failure_is_counted_rather_than_raised(self):
        broken = MutationAudit(Path(self._directory.name) / "nested" / "x")
        broken.open()
        broken.close()
        Path(broken.database_path).write_text("not a database", encoding="utf-8")

        result = broken.record(
            tool_call_id="call_1",
            session_id="session_1",
            turn_id="turn_1",
            workspace_id="workspace_1",
            tool_name="write_file",
            path="a.py",
            kind="create",
            before=None,
            after=b"x",
            lines_added=1,
            lines_removed=0,
        )

        self.assertIsNone(result)
        self.assertEqual(broken.failures, 1)

    def test_an_unsupported_database_version_is_refused(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA user_version = 99")
        finally:
            connection.close()

        with self.assertRaises(AuditUnavailableError):
            self.audit.open()

    def test_a_non_path_database_is_rejected(self):
        with self.assertRaises(TypeError):
            MutationAudit("mutations.sqlite3")  # type: ignore[arg-type]


class MutationRecordTests(unittest.TestCase):
    def _record(self, **overrides) -> MutationRecord:
        payload = {
            "record_id": "mut_1",
            "tool_call_id": "call_1",
            "session_id": "session_1",
            "turn_id": "turn_1",
            "workspace_id": "workspace_1",
            "tool_name": "write_file",
            "path": "a.py",
            "kind": "replace",
            "recorded_at": "2026-08-06T12:00:00+00:00",
            "before_sha256": "a" * 64,
            "after_sha256": "b" * 64,
            "before_bytes": 4,
            "after_bytes": 4,
            "lines_added": 1,
            "lines_removed": 1,
        }
        payload.update(overrides)
        return MutationRecord(**payload)  # type: ignore[arg-type]

    def test_a_created_file_cannot_carry_a_prior_digest(self):
        with self.assertRaises(ValueError):
            self._record(kind="create")

    def test_a_changed_file_requires_a_prior_digest(self):
        with self.assertRaises(ValueError):
            self._record(kind="edit", before_sha256=None)

    def test_an_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            self._record(kind="rename")


if __name__ == "__main__":
    unittest.main()
