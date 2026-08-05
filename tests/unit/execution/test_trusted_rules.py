from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers.platforms import requires_posix_permissions
from truecoder.execution.defaults import DEFAULT_EXECUTION_LIMITS
from truecoder.execution.models import RiskLevel
from truecoder.execution.trusted_rules import (
    MAX_RULES,
    TRUSTED_RULES_SCHEMA_VERSION,
    TrustedCommandRule,
    TrustedRulesError,
    TrustedRuleSet,
    apply_trusted_rules,
    load_trusted_rules,
    parse_trusted_rules,
    save_trusted_rules,
)


def rule(**overrides) -> TrustedCommandRule:
    values = {
        "rule_id": "pytest",
        "executable": "pytest",
        "max_risk": RiskLevel.LOW,
        "require_approval": False,
        **overrides,
    }
    return TrustedCommandRule(**values)


class RuleValidationTests(unittest.TestCase):
    def test_a_rule_must_name_a_bare_executable(self):
        with self.assertRaises(TrustedRulesError):
            rule(executable="/usr/bin/pytest")
        with self.assertRaises(TrustedRulesError):
            rule(executable="bin\\pytest.exe")

    def test_identifiers_reject_whitespace_and_null_bytes(self):
        with self.assertRaises(TrustedRulesError):
            rule(rule_id="two words")
        with self.assertRaises(TrustedRulesError):
            rule(executable="py\x00test")

    def test_empty_identifiers_are_refused(self):
        with self.assertRaises(TrustedRulesError):
            rule(rule_id="   ")

    def test_duplicate_rule_ids_are_refused(self):
        with self.assertRaises(TrustedRulesError):
            TrustedRuleSet(rules=(rule(), rule(executable="ruff")))

    def test_duplicate_executables_are_refused(self):
        with self.assertRaises(TrustedRulesError):
            TrustedRuleSet(rules=(rule(), rule(rule_id="other")))

    def test_an_unknown_schema_version_is_refused(self):
        with self.assertRaises(TrustedRulesError):
            TrustedRuleSet(version=TRUSTED_RULES_SCHEMA_VERSION + 1)

    def test_the_rule_count_is_bounded(self):
        rules = tuple(
            rule(rule_id=f"rule-{index}", executable=f"tool{index}")
            for index in range(MAX_RULES + 1)
        )
        with self.assertRaises(TrustedRulesError):
            TrustedRuleSet(rules=rules)


class ParsingTests(unittest.TestCase):
    def test_an_empty_document_parses_to_no_rules(self):
        parsed = parse_trusted_rules(json.dumps({"version": 1, "rules": []}))

        self.assertEqual(parsed.rules, ())

    def test_malformed_json_is_refused_with_a_clear_error(self):
        with self.assertRaises(TrustedRulesError):
            parse_trusted_rules("{not json")

    def test_a_non_object_document_is_refused(self):
        with self.assertRaises(TrustedRulesError):
            parse_trusted_rules("[]")

    def test_unknown_fields_are_refused_rather_than_ignored(self):
        document = json.dumps(
            {
                "version": 1,
                "rules": [
                    {
                        "rule_id": "a",
                        "executable": "pytest",
                        "max_risk": "low",
                        "escalate": True,
                    }
                ],
            }
        )
        with self.assertRaises(TrustedRulesError):
            parse_trusted_rules(document)

    def test_an_unknown_risk_level_is_refused(self):
        document = json.dumps(
            {
                "version": 1,
                "rules": [
                    {"rule_id": "a", "executable": "pytest", "max_risk": "trivial"}
                ],
            }
        )
        with self.assertRaises(TrustedRulesError):
            parse_trusted_rules(document)

    def test_approval_defaults_to_required_when_omitted(self):
        document = json.dumps(
            {
                "version": 1,
                "rules": [
                    {"rule_id": "a", "executable": "pytest", "max_risk": "low"}
                ],
            }
        )

        parsed = parse_trusted_rules(document)

        self.assertTrue(parsed.rules[0].require_approval)

    def test_an_oversized_document_is_refused(self):
        with self.assertRaises(TrustedRulesError):
            parse_trusted_rules("x" * (1024 * 1024 + 1))

    def test_a_document_round_trips_through_json(self):
        original = TrustedRuleSet(rules=(rule(), rule(rule_id="ruff", executable="ruff")))

        self.assertEqual(parse_trusted_rules(original.to_json()), original)


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "trusted-commands.json"

    def test_a_missing_file_is_an_empty_rule_set_not_an_error(self):
        self.assertEqual(load_trusted_rules(self.path).rules, ())

    def test_saving_then_loading_preserves_the_rules(self):
        rules = TrustedRuleSet(rules=(rule(),))
        save_trusted_rules(rules, self.path)

        self.assertEqual(load_trusted_rules(self.path), rules)

    @requires_posix_permissions
    def test_saved_rules_are_private_to_the_user(self):
        save_trusted_rules(TrustedRuleSet(rules=(rule(),)), self.path)

        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_a_corrupt_file_is_refused_rather_than_silently_ignored(self):
        self.path.write_text("{ broken", encoding="utf-8")

        with self.assertRaises(TrustedRulesError):
            load_trusted_rules(self.path)

    def test_saving_replaces_atomically_without_leaving_a_temporary(self):
        save_trusted_rules(TrustedRuleSet(rules=(rule(),)), self.path)

        siblings = [item.name for item in self.path.parent.iterdir()]
        self.assertEqual(siblings, [self.path.name])


class ApplicationTests(unittest.TestCase):
    def apply(self, rules: TrustedRuleSet, **overrides):
        values = {
            "executable": "pytest",
            "risk": RiskLevel.LOW,
            "requires_approval": True,
            "ceiling": DEFAULT_EXECUTION_LIMITS,
            "requested": DEFAULT_EXECUTION_LIMITS,
            **overrides,
        }
        return apply_trusted_rules(rules, **values)

    def test_an_unmatched_command_is_left_untouched(self):
        risk, approval, reasons = self.apply(
            TrustedRuleSet(), executable="rm"
        )

        self.assertIs(risk, RiskLevel.LOW)
        self.assertTrue(approval)
        self.assertEqual(reasons, ())

    def test_a_matching_low_risk_rule_can_waive_approval(self):
        risk, approval, reasons = self.apply(TrustedRuleSet(rules=(rule(),)))

        self.assertIs(risk, RiskLevel.LOW)
        self.assertFalse(approval)
        self.assertEqual(reasons, ("policy.020.trusted.applied.pytest",))

    def test_a_rule_never_lowers_a_risk_level_it_does_not_cover(self):
        risk, approval, reasons = self.apply(
            TrustedRuleSet(rules=(rule(),)),
            risk=RiskLevel.HIGH,
        )

        self.assertIs(risk, RiskLevel.HIGH)
        self.assertTrue(approval)
        self.assertEqual(reasons, ("policy.020.trusted.rejected.pytest",))

    def test_approval_is_retained_above_low_risk_even_when_waived(self):
        rules = TrustedRuleSet(
            rules=(rule(max_risk=RiskLevel.HIGH, require_approval=False),)
        )

        risk, approval, reasons = self.apply(rules, risk=RiskLevel.MEDIUM)

        self.assertIs(risk, RiskLevel.MEDIUM)
        self.assertTrue(approval)
        self.assertEqual(
            reasons,
            ("policy.020.trusted.approval_retained.pytest",),
        )

    def test_a_rule_cannot_introduce_approval_where_policy_wanted_none(self):
        rules = TrustedRuleSet(rules=(rule(require_approval=True),))

        _, approval, _ = self.apply(rules, requires_approval=False)

        self.assertFalse(approval)


if __name__ == "__main__":
    unittest.main()
