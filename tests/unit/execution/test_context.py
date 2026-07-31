from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from truecoder.execution.context import ExecutionContextFactory, workspace_id_for

UTC_NOW = datetime(2026, 7, 31, 8, 30, tzinfo=timezone.utc)


class ExecutionContextFactoryTests(unittest.TestCase):
    def test_creates_correlated_unique_execution_contexts(self):
        with tempfile.TemporaryDirectory() as directory:
            identifiers = iter(("exec_one", "exec_two"))
            factory = ExecutionContextFactory(
                execution_id_factory=lambda: next(identifiers),
                clock=lambda: UTC_NOW,
            )

            first = factory.create(
                tool_call_id="call_01",
                session_id="session_01",
                turn_id="turn_01",
                project_root=Path(directory),
            )
            second = factory.create(
                tool_call_id="call_02",
                session_id="session_01",
                turn_id="turn_01",
                project_root=Path(directory),
            )

        self.assertEqual(first.execution_id, "exec_one")
        self.assertEqual(second.execution_id, "exec_two")
        self.assertEqual(first.session_id, second.session_id)
        self.assertEqual(first.turn_id, second.turn_id)
        self.assertEqual(first.workspace_id, second.workspace_id)
        self.assertEqual(first.launched_at_utc, UTC_NOW)

    def test_rejects_missing_runtime_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = ExecutionContextFactory()
            values = (
                {"tool_call_id": "", "session_id": "session", "turn_id": "turn"},
                {"tool_call_id": "call", "session_id": "", "turn_id": "turn"},
                {"tool_call_id": "call", "session_id": "session", "turn_id": ""},
            )

            for identities in values:
                with (
                    self.subTest(identities=identities),
                    self.assertRaises(ValueError),
                ):
                    factory.create(
                        **identities,
                        project_root=Path(directory),
                    )

    def test_workspace_identity_uses_the_canonical_host_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "child").mkdir()

            canonical = workspace_id_for(root)
            aliased = workspace_id_for(root / "child" / "..")

        self.assertEqual(canonical, aliased)
        self.assertTrue(canonical.startswith("workspace_"))

    def test_different_workspaces_have_different_identities(self):
        with (
            tempfile.TemporaryDirectory() as first_directory,
            tempfile.TemporaryDirectory() as second_directory,
        ):
            first = workspace_id_for(Path(first_directory))
            second = workspace_id_for(Path(second_directory))

        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
