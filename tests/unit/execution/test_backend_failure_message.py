"""An unsatisfiable request must say which capability nothing could provide."""

from __future__ import annotations

import unittest

from truecoder.execution.errors import (
    ExecutionInfrastructureError,
    NoCompatibleBackendError,
    describe_backend_failures,
)

FAILURES = (
    ("posix", ("Backend 'posix' does not support filesystem mode 'workspace-read'.",)),
    ("container", ("Backend 'container' is unavailable.",)),
)


class BackendFailureMessageTests(unittest.TestCase):
    def test_each_rejected_backend_and_reason_reaches_the_message(self):
        error = NoCompatibleBackendError(failures=FAILURES, preference="auto")

        message = str(error)
        self.assertIn("posix", message)
        self.assertIn("workspace-read", message)
        self.assertIn("container", message)
        self.assertIn("unavailable", message)

    def test_several_reasons_for_one_backend_are_joined(self):
        error = NoCompatibleBackendError(
            failures=(("posix", ("no filesystem mode", "no network isolation")),),
        )

        self.assertIn("no filesystem mode and no network isolation", str(error))

    def test_a_backend_without_reasons_is_still_named(self):
        described = describe_backend_failures("nothing fits", (("posix", ()),))

        self.assertEqual(described, "nothing fits: posix was rejected")

    def test_no_failures_leaves_the_message_alone(self):
        error = NoCompatibleBackendError()

        self.assertEqual(str(error), "no compatible execution backend is available")

    def test_the_failures_stay_available_as_data(self):
        error = NoCompatibleBackendError(failures=FAILURES)

        self.assertEqual(error.failures, FAILURES)
        self.assertIsInstance(error, ExecutionInfrastructureError)


if __name__ == "__main__":
    unittest.main()
