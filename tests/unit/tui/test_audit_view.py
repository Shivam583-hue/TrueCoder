from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from truecoder.tui.audit_view import (
    MAX_PREVIEW_CHARS,
    REDACTED,
    AuditFilter,
    AuditRow,
    bounded_preview,
    filter_rows,
    recent_cutoff,
    sanitize_detail_rows,
    summarize,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def audit_row(**overrides) -> AuditRow:
    values = {
        "run_id": "run-1",
        "audit_id": "audit-1",
        "command": "pytest -q",
        "backend": "posix",
        "outcome": "completed",
        "exit_code": 0,
        "updated_at_utc": NOW,
        "cleanup_complete": True,
        **overrides,
    }
    return AuditRow(**values)


class AuditRowTests(unittest.TestCase):
    def test_empty_identifiers_are_refused(self):
        with self.assertRaises(ValueError):
            audit_row(run_id="  ")

    def test_naive_timestamps_are_refused(self):
        with self.assertRaises(ValueError):
            audit_row(updated_at_utc=datetime(2026, 8, 4))  # noqa: DTZ001

    def test_incomplete_cleanup_is_surfaced_over_the_exit_code(self):
        row = audit_row(cleanup_complete=False)

        self.assertIn("cleanup incomplete", row.status_label())
        self.assertNotIn("exit 0", row.status_label())

    def test_a_normal_run_shows_its_exit_code(self):
        self.assertEqual(audit_row().status_label(), "completed · exit 0")

    def test_a_run_without_an_exit_code_shows_only_the_outcome(self):
        self.assertEqual(
            audit_row(outcome="denied", exit_code=None).status_label(),
            "denied",
        )

    def test_every_shipped_outcome_is_terminal(self):
        for outcome in ("completed", "failed", "timed_out", "cancelled"):
            self.assertTrue(audit_row(outcome=outcome).terminal)


class AuditFilterTests(unittest.TestCase):
    def test_an_unknown_outcome_is_refused(self):
        with self.assertRaises(ValueError):
            AuditFilter(outcome="exploded")

    def test_a_naive_since_value_is_refused(self):
        with self.assertRaises(ValueError):
            AuditFilter(since_utc=datetime(2026, 8, 1))  # noqa: DTZ001

    def test_outcome_filtering_selects_only_matching_rows(self):
        rows = (audit_row(), audit_row(run_id="run-2", outcome="failed"))

        selected = filter_rows(rows, AuditFilter(outcome="failed"))

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].outcome, "failed")

    def test_backend_filtering_selects_only_matching_rows(self):
        rows = (audit_row(), audit_row(run_id="run-2", backend="container"))

        selected = filter_rows(rows, AuditFilter(backend="container"))

        self.assertEqual(len(selected), 1)

    def test_since_filtering_excludes_older_rows(self):
        rows = (
            audit_row(run_id="old", updated_at_utc=NOW - timedelta(days=5)),
            audit_row(run_id="new"),
        )

        selected = filter_rows(rows, AuditFilter(since_utc=NOW - timedelta(days=1)))

        self.assertEqual([row.run_id for row in selected], ["new"])

    def test_search_matches_command_and_identifiers_case_insensitively(self):
        rows = (audit_row(command="RUFF check"),)

        self.assertEqual(len(filter_rows(rows, AuditFilter(search="ruff"))), 1)
        self.assertEqual(len(filter_rows(rows, AuditFilter(search="audit-1"))), 1)
        self.assertEqual(len(filter_rows(rows, AuditFilter(search="absent"))), 0)

    def test_an_overlong_search_is_refused(self):
        with self.assertRaises(ValueError):
            AuditFilter(search="x" * 201)


class FilteringTests(unittest.TestCase):
    def test_rows_are_returned_newest_first(self):
        rows = (
            audit_row(run_id="old", updated_at_utc=NOW - timedelta(days=2)),
            audit_row(run_id="new"),
        )

        self.assertEqual(
            [row.run_id for row in filter_rows(rows)],
            ["new", "old"],
        )

    def test_the_row_count_is_bounded(self):
        rows = tuple(
            audit_row(run_id=f"run-{index}", audit_id=f"audit-{index}")
            for index in range(500)
        )

        self.assertLessEqual(len(filter_rows(rows)), 200)

    def test_an_explicit_limit_is_honoured(self):
        rows = tuple(
            audit_row(run_id=f"run-{index}", audit_id=f"audit-{index}")
            for index in range(50)
        )

        self.assertEqual(len(filter_rows(rows, limit=5)), 5)

    def test_a_non_positive_limit_is_refused(self):
        with self.assertRaises(ValueError):
            filter_rows((), limit=0)


class SanitizationTests(unittest.TestCase):
    def test_secret_shaped_names_are_redacted(self):
        details = (
            ("API_KEY", "super-secret"),
            ("aws_secret_access_key", "leak"),
            ("environment_values", "many"),
            ("backend", "posix"),
        )

        sanitized = dict(sanitize_detail_rows(details))

        self.assertEqual(sanitized["API_KEY"], REDACTED)
        self.assertEqual(sanitized["aws_secret_access_key"], REDACTED)
        self.assertEqual(sanitized["environment_values"], REDACTED)
        self.assertEqual(sanitized["backend"], "posix")

    def test_ordinary_values_are_bounded(self):
        sanitized = dict(sanitize_detail_rows((("stdout", "x" * 5000),)))

        self.assertEqual(len(sanitized["stdout"]), MAX_PREVIEW_CHARS)

    def test_previews_are_bounded(self):
        self.assertEqual(len(bounded_preview("y" * 9000)), MAX_PREVIEW_CHARS)
        self.assertEqual(bounded_preview("short"), "short")

    def test_non_string_details_are_refused(self):
        with self.assertRaises(TypeError):
            sanitize_detail_rows((("name", 3),))  # type: ignore[arg-type]


class SummaryTests(unittest.TestCase):
    def test_an_empty_store_says_so(self):
        self.assertEqual(summarize(()), "No audit runs recorded")

    def test_incomplete_cleanup_is_called_out(self):
        rows = (audit_row(), audit_row(run_id="run-2", cleanup_complete=False))

        self.assertIn("1 with incomplete cleanup", summarize(rows))

    def test_a_healthy_store_reports_only_the_count(self):
        self.assertEqual(summarize((audit_row(),)), "1 runs")

    def test_recent_cutoff_requires_a_positive_window(self):
        with self.assertRaises(ValueError):
            recent_cutoff(0)

    def test_recent_cutoff_is_the_window_before_now(self):
        self.assertEqual(recent_cutoff(7, NOW), NOW - timedelta(days=7))


if __name__ == "__main__":
    unittest.main()
