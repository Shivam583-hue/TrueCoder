from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from truecoder.execution.audit.retention import (
    MAX_RETENTION_DAYS,
    RetentionError,
    RetentionPolicy,
    plan_retention,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def row(run_id: str, age_days: float, terminal: bool = True):
    return (run_id, NOW - timedelta(days=age_days), terminal)


class RetentionPolicyTests(unittest.TestCase):
    def test_a_zero_or_negative_window_is_refused(self):
        with self.assertRaises(RetentionError):
            RetentionPolicy(days=0)
        with self.assertRaises(RetentionError):
            RetentionPolicy(days=-1)

    def test_an_absurd_window_is_refused(self):
        with self.assertRaises(RetentionError):
            RetentionPolicy(days=MAX_RETENTION_DAYS + 1)

    def test_a_naive_timestamp_is_refused(self):
        with self.assertRaises(RetentionError):
            RetentionPolicy().cutoff(datetime(2026, 8, 4))  # noqa: DTZ001

    def test_the_cutoff_is_the_window_before_now(self):
        cutoff = RetentionPolicy(days=7).cutoff(NOW)

        self.assertEqual(cutoff, NOW - timedelta(days=7))


class RetentionPlanTests(unittest.TestCase):
    def test_recent_runs_are_never_deleted(self):
        rows = (row("recent", age_days=1),)

        deletable, report = plan_retention(rows, RetentionPolicy(days=30), now=NOW)

        self.assertEqual(deletable, ())
        self.assertEqual(report.deleted, 0)
        self.assertEqual(report.examined, 1)

    def test_old_terminal_runs_are_deletable(self):
        rows = (row("old", age_days=45),)

        deletable, report = plan_retention(rows, RetentionPolicy(days=30), now=NOW)

        self.assertEqual(deletable, ("old",))
        self.assertEqual(report.deleted, 1)

    def test_old_nonterminal_runs_are_retained_for_investigation(self):
        rows = (row("stuck", age_days=45, terminal=False),)

        deletable, report = plan_retention(rows, RetentionPolicy(days=30), now=NOW)

        self.assertEqual(deletable, ())
        self.assertEqual(report.retained_nonterminal, 1)

    def test_nonterminal_retention_can_be_disabled_explicitly(self):
        rows = (row("stuck", age_days=45, terminal=False),)
        policy = RetentionPolicy(days=30, keep_nonterminal=False)

        deletable, report = plan_retention(rows, policy, now=NOW)

        self.assertEqual(deletable, ("stuck",))
        self.assertEqual(report.retained_nonterminal, 0)

    def test_a_run_exactly_at_the_cutoff_is_kept(self):
        rows = (row("edge", age_days=30),)

        deletable, _ = plan_retention(rows, RetentionPolicy(days=30), now=NOW)

        self.assertEqual(deletable, ())

    def test_mixed_rows_are_partitioned_correctly(self):
        rows = (
            row("keep-recent", age_days=1),
            row("delete-old", age_days=90),
            row("keep-stuck", age_days=90, terminal=False),
        )

        deletable, report = plan_retention(rows, RetentionPolicy(days=30), now=NOW)

        self.assertEqual(deletable, ("delete-old",))
        self.assertEqual(report.examined, 3)
        self.assertEqual(report.deleted, 1)
        self.assertEqual(report.retained_nonterminal, 1)

    def test_naive_row_timestamps_are_refused(self):
        rows = (("bad", datetime(2026, 1, 1), True),)  # noqa: DTZ001

        with self.assertRaises(RetentionError):
            plan_retention(rows, RetentionPolicy(), now=NOW)

    def test_empty_run_identifiers_are_refused(self):
        rows = (("  ", NOW - timedelta(days=90), True),)

        with self.assertRaises(RetentionError):
            plan_retention(rows, RetentionPolicy(), now=NOW)

    def test_the_report_can_never_claim_more_than_it_examined(self):
        rows = tuple(row(f"run-{index}", age_days=90) for index in range(5))

        _, report = plan_retention(rows, RetentionPolicy(days=30), now=NOW)

        self.assertLessEqual(report.deleted, report.examined)


if __name__ == "__main__":
    unittest.main()
