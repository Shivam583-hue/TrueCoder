from __future__ import annotations

import unittest
from pathlib import Path

from truecoder.execution.approval import ExecutionApprovalDetails
from truecoder.execution.models import (
    EXECUTION_LIFECYCLE_STAGES,
    BackendCapabilities,
    ExecutionLimits,
    ExecutionRequest,
    RiskLevel,
)
from truecoder.tui.execution_view import (
    MAX_PREVIEW_LINE_CHARS,
    TRUNCATION_NOTE,
    BoundedPreview,
    compact_approval_rows,
    full_approval_rows,
    is_terminal_stage,
    scope_label,
    stage_presentation,
)


def capabilities() -> BackendCapabilities:
    return BackendCapabilities(
        filesystem_isolation="enforced",
        network_isolation="enforced",
        memory_limits="enforced",
        cpu_limits="best_effort",
        process_limits="enforced",
        timeout_enforcement="enforced",
        cancellation="enforced",
        supported_execution_modes=("exec", "shell"),
        supported_filesystem_modes=("workspace-read", "workspace-write"),
        supported_shells=("posix",),
    )


def details(
    *,
    filesystem_mode: str = "workspace-read",
    network_access: bool = False,
    memory_bytes: int | None = None,
    cpu_seconds: float | None = None,
    max_processes: int | None = None,
) -> ExecutionApprovalDetails:
    request = ExecutionRequest(
        mode="exec",
        argv=("pytest", "-q"),
        script=None,
        working_directory=Path.cwd().resolve(),
        limits=ExecutionLimits(
            timeout_seconds=120,
            max_output_bytes=1024 * 1024,
            max_return_bytes=64 * 1024,
            memory_bytes=memory_bytes,
            cpu_seconds=cpu_seconds,
            max_processes=max_processes,
        ),
        network_access=network_access,
        filesystem_mode=filesystem_mode,  # type: ignore[arg-type]
    )
    return ExecutionApprovalDetails(
        execution_id="exec-1",
        command_display="pytest -q",
        request=request,
        backend="container",
        capabilities=capabilities(),
        risk=RiskLevel.MEDIUM,
        reasons=("known-test-command",),
        policy_version="truecoder-execution-v1",
    )


class StagePresentationTests(unittest.TestCase):
    def test_every_lifecycle_stage_has_a_presentation(self):
        for stage in EXECUTION_LIFECYCLE_STAGES:
            presentation = stage_presentation(stage)  # type: ignore[arg-type]
            self.assertTrue(presentation.label)
            self.assertTrue(presentation.glyph)
            self.assertTrue(presentation.state)

    def test_unknown_stage_is_rejected_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            stage_presentation("invented")  # type: ignore[arg-type]

    def test_terminal_stages_match_the_execution_vocabulary(self):
        terminal = {
            stage
            for stage in EXECUTION_LIFECYCLE_STAGES
            if is_terminal_stage(stage)  # type: ignore[arg-type]
        }
        self.assertEqual(
            terminal,
            {
                "completed",
                "failed",
                "timed_out",
                "cancelled",
                "denied",
                "limit_exceeded",
                "failed_to_start",
            },
        )

    def test_a_timeout_is_not_presented_as_a_plain_failure(self):
        self.assertEqual(stage_presentation("timed_out").label, "Timed out")
        self.assertEqual(stage_presentation("failed").label, "Failed")

    def test_a_policy_denial_is_not_presented_as_a_crash(self):
        self.assertEqual(stage_presentation("denied").state, "rejected")
        self.assertEqual(stage_presentation("failed_to_start").label, "Never started")


class CompactApprovalTests(unittest.TestCase):
    def test_compact_view_is_seven_rows_in_decision_order(self):
        rows = compact_approval_rows(details())

        self.assertEqual(
            tuple(name for name, _ in rows),
            (
                "Command",
                "Directory",
                "Backend",
                "Access",
                "Limits",
                "Risk",
                "Approval",
            ),
        )

    def test_access_row_states_filesystem_and_network_plainly(self):
        rows = dict(compact_approval_rows(details()))
        self.assertEqual(rows["Access"], "workspace read-only · network denied")

        permissive = dict(
            compact_approval_rows(
                details(filesystem_mode="host", network_access=True)
            )
        )
        self.assertEqual(
            permissive["Access"],
            "full host filesystem · network allowed",
        )

    def test_limits_row_omits_limits_that_were_not_requested(self):
        rows = dict(compact_approval_rows(details()))
        self.assertEqual(rows["Limits"], "120s timeout · 1.0 MiB output")

        bounded = dict(
            compact_approval_rows(
                details(memory_bytes=512 * 1024 * 1024, max_processes=64)
            )
        )
        self.assertEqual(
            bounded["Limits"],
            "120s timeout · 1.0 MiB output · 512.0 MiB memory · 64 processes",
        )

    def test_approval_row_names_the_scopes_policy_allows(self):
        once = dict(compact_approval_rows(details(), ("once",)))
        self.assertEqual(once["Approval"], "this run only")

        broad = dict(
            compact_approval_rows(details(), ("once", "session", "workspace"))
        )
        self.assertEqual(
            broad["Approval"],
            "this run only, this session, this workspace",
        )

    def test_scope_label_handles_an_empty_scope_set(self):
        self.assertEqual(scope_label(()), "no scope available")

    def test_full_view_keeps_every_capability_field(self):
        rows = dict(full_approval_rows(details()))

        self.assertEqual(rows["Filesystem isolation"], "enforced")
        self.assertEqual(rows["CPU enforcement"], "best_effort")
        self.assertEqual(rows["Memory limit"], "not requested")
        self.assertGreaterEqual(len(rows), 24)


class BoundedPreviewTests(unittest.TestCase):
    def test_streamed_chunks_reassemble_across_boundaries(self):
        preview = BoundedPreview()
        preview.append("first li")
        preview.append("ne\nsecond line\n")

        self.assertEqual(preview.text(), "first line\nsecond line")

    def test_an_incomplete_final_line_is_still_shown(self):
        preview = BoundedPreview()
        preview.append("done\nworking")

        self.assertEqual(preview.text(), "done\nworking")

    def test_carriage_returns_are_normalized(self):
        preview = BoundedPreview()
        preview.append("a\r\nb\rc\n")

        self.assertEqual(preview.text(), "a\nb\nc")

    def test_only_the_recent_tail_is_retained(self):
        preview = BoundedPreview(max_lines=3)
        preview.append("".join(f"line {index}\n" for index in range(10)))

        text = preview.text()
        self.assertIn(TRUNCATION_NOTE, text)
        self.assertIn("line 9", text)
        self.assertNotIn("line 0", text)
        self.assertTrue(preview.trimmed)

    def test_a_single_enormous_line_cannot_grow_without_bound(self):
        preview = BoundedPreview()
        preview.append("x" * (MAX_PREVIEW_LINE_CHARS * 4) + "\n")

        line = preview.text()
        self.assertEqual(len(line), MAX_PREVIEW_LINE_CHARS)
        self.assertTrue(line.endswith("…"))

    def test_memory_stays_bounded_across_many_appends(self):
        preview = BoundedPreview(max_lines=5)
        for index in range(2000):
            preview.append(f"chunk {index}\n")

        self.assertLessEqual(len(preview.text().splitlines()), 6)

    def test_empty_preview_renders_nothing(self):
        self.assertEqual(BoundedPreview().text(), "")

    def test_clear_resets_the_trim_marker(self):
        preview = BoundedPreview(max_lines=1)
        preview.append("a\nb\n")
        preview.clear()

        self.assertEqual(preview.text(), "")
        self.assertFalse(preview.trimmed)

    def test_construction_validates_its_bounds(self):
        with self.assertRaises(ValueError):
            BoundedPreview(max_lines=0)
        with self.assertRaises(TypeError):
            BoundedPreview(max_line_chars=True)


if __name__ == "__main__":
    unittest.main()
