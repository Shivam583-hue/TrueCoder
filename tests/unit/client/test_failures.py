"""A provider's refusal must read as a sentence, not as its wire format."""

from __future__ import annotations

import unittest

from truecoder.client.failures import (
    BILLING,
    CREDENTIAL,
    MAX_DETAIL_CHARS,
    NETWORK,
    PARTIAL_NOTICE,
    PROVIDER,
    RATE_LIMIT,
    TIMEOUT,
    UNKNOWN_MODEL,
    ProviderFailure,
    bounded,
    classify,
    classify_exception,
    named,
    provider_message,
    remedy,
    timed_out,
    unreachable,
)

CREDITS = (
    "This request requires more credits, or fewer max_tokens. You requested up "
    "to 65536 tokens, but can only afford 3325."
)


class _Refusal:
    def __init__(self, status, body) -> None:
        self.status_code = status
        self.body = body

    def __str__(self) -> str:
        return f"Error code: {self.status_code} - {self.body}"


class MessageExtractionTests(unittest.TestCase):
    def test_the_nested_provider_message_is_lifted_out(self):
        body = {"error": {"message": CREDITS, "code": 402, "metadata": {"a": "b"}}}

        self.assertEqual(provider_message(body), CREDITS)

    def test_a_top_level_message_is_accepted(self):
        self.assertEqual(provider_message({"message": "no"}), "no")

    def test_a_string_error_is_accepted(self):
        self.assertEqual(provider_message({"error": "denied"}), "denied")

    def test_a_shapeless_body_yields_nothing(self):
        self.assertEqual(provider_message(None), "")
        self.assertEqual(provider_message([1, 2]), "")
        self.assertEqual(provider_message({"error": {"code": 402}}), "")

    def test_a_long_message_is_bounded(self):
        bounded_text = bounded("x" * (MAX_DETAIL_CHARS * 3))

        self.assertLess(len(bounded_text), MAX_DETAIL_CHARS * 2)
        self.assertTrue(bounded_text.endswith("...[truncated]"))

    def test_newlines_never_reach_the_transcript_as_layout(self):
        self.assertEqual(bounded("one\n\n  two\tthree"), "one two three")


class ClassificationTests(unittest.TestCase):
    def test_a_rejected_key_is_a_credential_failure(self):
        failure = classify(status=401, provider="acme")

        self.assertEqual(failure.kind, CREDENTIAL)
        self.assertIn("acme", failure.summary)

    def test_a_forbidden_response_is_also_a_credential_failure(self):
        self.assertEqual(classify(status=403, provider="acme").kind, CREDENTIAL)

    def test_running_out_of_credit_is_not_a_credential_failure(self):
        failure = classify(
            status=402,
            body={"error": {"message": CREDITS}},
            provider="openrouter",
        )

        self.assertEqual(failure.kind, BILLING)
        self.assertIn("billing", failure.summary)
        self.assertIn("65536 tokens", failure.message)

    def test_rate_limiting_is_its_own_kind(self):
        self.assertEqual(classify(status=429, provider="acme").kind, RATE_LIMIT)

    def test_an_unknown_model_names_the_model(self):
        failure = classify(status=404, provider="acme", model="acme/ghost")

        self.assertEqual(failure.kind, UNKNOWN_MODEL)
        self.assertIn("acme/ghost", failure.summary)

    def test_a_server_fault_names_the_status(self):
        failure = classify(status=503, provider="acme")

        self.assertEqual(failure.kind, PROVIDER)
        self.assertIn("503", failure.summary)

    def test_a_statusless_failure_still_reads_as_a_sentence(self):
        failure = classify(status=None, fallback="bad response", provider="acme")

        self.assertEqual(failure.kind, PROVIDER)
        self.assertTrue(failure.summary.endswith("."))
        self.assertEqual(failure.detail, "bad response")

    def test_the_unnamed_provider_is_never_called_default(self):
        self.assertEqual(named("default"), "The provider")
        self.assertEqual(named(""), "The provider")
        self.assertEqual(named("openrouter"), "openrouter")

    def test_a_partial_reply_says_the_answer_was_cut_short(self):
        failure = classify(status=429, provider="acme", partial=True)

        self.assertIn(PARTIAL_NOTICE, failure.summary)

    def test_an_exception_is_read_for_its_status_and_body(self):
        error = _Refusal(402, {"error": {"message": CREDITS}})

        failure = classify_exception(error, provider="openrouter")

        self.assertEqual(failure.kind, BILLING)
        self.assertEqual(failure.status, 402)
        self.assertEqual(failure.detail, CREDITS)

    def test_an_exception_without_a_status_falls_back_to_its_text(self):
        failure = classify_exception(RuntimeError("boom"), provider="acme")

        self.assertEqual(failure.kind, PROVIDER)
        self.assertEqual(failure.detail, "boom")

    def test_the_raw_wire_format_never_reaches_the_message(self):
        error = _Refusal(402, {"error": {"message": CREDITS, "metadata": {"x": "y"}}})

        message = classify_exception(error, provider="openrouter").message

        self.assertNotIn("{", message)
        self.assertNotIn("'code'", message)
        self.assertNotIn("metadata", message)

    def test_a_timeout_and_a_dead_connection_are_distinct(self):
        self.assertEqual(timed_out("acme").kind, TIMEOUT)
        self.assertEqual(unreachable("acme").kind, NETWORK)

    def test_a_failure_with_no_detail_is_one_sentence(self):
        failure = ProviderFailure(kind=PROVIDER, summary="acme failed.")

        self.assertEqual(failure.message, "acme failed.")


class RemedyTests(unittest.TestCase):
    def test_an_oauth_provider_is_told_to_run_login(self):
        self.assertIn("/login", remedy(CREDENTIAL, oauth=True))

    def test_a_key_provider_is_told_about_the_prompt(self):
        advice = remedy(CREDENTIAL, oauth=False)

        self.assertIn("key", advice)
        self.assertNotIn("sign in", advice)

    def test_billing_points_at_credit_rather_than_credentials(self):
        advice = remedy(BILLING)

        self.assertIn("credit", advice)
        self.assertNotIn("/login", advice)

    def test_an_unknown_model_points_at_the_picker(self):
        self.assertIn("/models", remedy(UNKNOWN_MODEL))

    def test_an_unclassified_failure_offers_no_false_advice(self):
        self.assertEqual(remedy(PROVIDER), "")
        self.assertEqual(remedy(""), "")


if __name__ == "__main__":
    unittest.main()
