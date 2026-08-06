from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from tests.integration.tui.test_app import FakeLLMClient, make_agent
from truecoder.execution.audit.models import AuditRunAdmission
from truecoder.execution.audit.service import AuditService
from truecoder.execution.audit.store import SQLiteAuditStore
from truecoder.execution.bootstrap import ExecutionHealthReport, ExecutionRuntime
from truecoder.execution.context import workspace_id_for
from truecoder.tui.app import TrueCoderApp
from truecoder.tui.audit_view import (
    AuditListItem,
    AuditViewerScreen,
)


class AuditViewerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ctrl_a_opens_workspace_audit_and_escape_closes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = SQLiteAuditStore(root / "audit.sqlite3")
            store.create_pending(
                AuditRunAdmission(
                    run_id="run-1",
                    execution_id="exec-1",
                    tool_call_id="call-1",
                    session_id="session-1",
                    turn_id="turn-1",
                    workspace_id=workspace_id_for(Path.cwd().resolve()),
                    request_sha256="1" * 64,
                    request_summary=(
                        ("command", "pytest -q"),
                        ("backend", "auto"),
                    ),
                    created_at=datetime.now(UTC),
                )
            )
            agent = make_agent(FakeLLMClient([]))
            app = TrueCoderApp(agent)
            agent._execution_runtime = ExecutionRuntime(
                service=None,
                audit=AuditService(store),
                discovery=None,
                backends=(),
                health=ExecutionHealthReport(
                    enabled=False,
                    audit_ready=True,
                    recovery_ready=True,
                    backends=(),
                    failure_code="execution_disabled",
                ),
            )
            agent._execution_initialized = True

            async with app.run_test(size=(120, 40)) as pilot:
                base_screen = app.screen
                await pilot.press("ctrl+a")
                await pilot.pause()

                self.assertIsInstance(app.screen, AuditViewerScreen)
                items = list(app.screen.query(AuditListItem))
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0].row.command, "pytest -q")

                await pilot.press("escape")
                await pilot.pause()

                self.assertIs(app.screen, base_screen)

    async def test_filters_update_the_visible_list(self):
        rows = (
            self._row("run-1", "pytest -q", "completed", "posix"),
            self._row("run-2", "ruff check", "failed", "container"),
        )
        app = TrueCoderApp(make_agent(FakeLLMClient([])))

        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(AuditViewerScreen(rows))
            await pilot.pause()
            await pilot.press("/")
            await pilot.press("r", "u", "f", "f")
            await pilot.pause()

            items = list(app.screen.query(AuditListItem))
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].row.run_id, "run-2")

    @staticmethod
    def _row(run_id: str, command: str, outcome: str, backend: str):
        from truecoder.tui.audit_view import AuditRow

        return AuditRow(
            run_id=run_id,
            command=command,
            backend=backend,
            outcome=outcome,
            exit_code=0 if outcome == "completed" else 1,
            updated_at_utc=datetime.now(UTC),
            cleanup_complete=True,
        )


if __name__ == "__main__":
    unittest.main()
