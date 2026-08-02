from __future__ import annotations

import random
import unittest

from truecoder.execution.environment import (
    REDACTED_VALUE,
    EnvironmentPolicy,
    classify_secret_name,
    construct_environment,
    redact_environment,
)


class SecretNameClassificationTests(unittest.TestCase):
    def test_classifies_exact_prefix_and_suffix_rules_case_insensitively(self):
        values = (
            ("GITHUB_TOKEN", "token"),
            ("openai_api_key", "credential"),
            ("service_password", "password"),
            ("AWS_REGION", "cloud"),
            ("Google_Application_Credentials", "cloud"),
        )
        for name, category in values:
            with self.subTest(name=name):
                match = classify_secret_name(name)
                self.assertTrue(match.sensitive)
                self.assertEqual(match.category, category)

    def test_avoids_common_non_secret_false_positives(self):
        for name in (
            "TOKENIZERS_PARALLELISM",
            "PASSWORD_POLICY",
            "SECRETARY",
            "API_KEYBOARD_LAYOUT",
            "PATH",
        ):
            with self.subTest(name=name):
                self.assertFalse(classify_secret_name(name).sensitive)

    def test_secret_name_classification_is_stable_under_random_casing(self):
        generator = random.Random(22)
        original = "SERVICE_PRIVATE_KEY"
        for _ in range(100):
            variation = "".join(
                character.upper()
                if generator.choice((True, False))
                else character.lower()
                for character in original
            )
            with self.subTest(variation=variation):
                self.assertEqual(
                    classify_secret_name(variation),
                    classify_secret_name(original),
                )


class EnvironmentConstructionTests(unittest.TestCase):
    def test_inherits_only_the_minimal_posix_environment(self):
        environment = construct_environment(
            platform="posix",
            inherited={
                "LANG": "C.UTF-8",
                "PATH": "/usr/bin",
                "HOME": "/home/person",
                "RANDOM_PROJECT_VALUE": "ignored",
                "OPENAI_API_KEY": "not-a-real-secret",
            },
            requested=(),
        )

        self.assertEqual(
            environment.variables,
            (("LANG", "C.UTF-8"), ("PATH", "/usr/bin")),
        )
        self.assertTrue(environment.valid)
        removed = {item.name: item.reason_code for item in environment.metadata.removed}
        self.assertEqual(removed["HOME"], "not-in-minimal-allowlist")
        self.assertEqual(removed["OPENAI_API_KEY"], "sensitive-credential")
        self.assertIn("not-a-real-secret", environment.redaction_values)
        self.assertNotIn("not-a-real-secret", repr(environment))

    def test_defined_and_requested_values_override_in_order(self):
        environment = construct_environment(
            platform="posix",
            inherited={"PATH": "/usr/bin", "LANG": "C"},
            defined=(("PATH", "/truecoder/bin"), ("TRUECODER", "1")),
            requested=(("PATH", "/project/bin"), ("CI", "1")),
        )

        self.assertEqual(
            dict(environment.variables),
            {
                "CI": "1",
                "LANG": "C",
                "PATH": "/project/bin",
                "TRUECODER": "1",
            },
        )
        self.assertEqual(environment.metadata.overridden_names, ("PATH",))

    def test_explicit_secret_values_are_removed_and_reported_as_violations(self):
        environment = construct_environment(
            platform="posix",
            inherited={"PATH": "/usr/bin"},
            requested=(("GITHUB_TOKEN", "not-a-real-token"),),
        )

        self.assertFalse(environment.valid)
        self.assertNotIn("GITHUB_TOKEN", dict(environment.variables))
        self.assertEqual(
            environment.violations[0].code,
            "sensitive-requested-environment",
        )
        self.assertEqual(
            environment.redaction_values,
            ("not-a-real-token",),
        )

    def test_windows_environment_names_are_case_insensitive(self):
        environment = construct_environment(
            platform="windows",
            inherited={"Path": r"C:\Windows", "SystemRoot": r"C:\Windows"},
            requested=(("PATH", r"C:\Project"),),
        )

        self.assertEqual(dict(environment.variables)["PATH"], r"C:\Project")
        with self.assertRaisesRegex(ValueError, "platform-equivalent duplicate"):
            construct_environment(
                platform="windows",
                inherited={"Path": "one", "PATH": "two"},
                requested=(),
            )

    def test_home_paths_require_an_explicit_policy_choice(self):
        without_home = construct_environment(
            platform="posix",
            inherited={"PATH": "/usr/bin", "HOME": "/home/person"},
            requested=(),
        )
        with_home = construct_environment(
            platform="posix",
            inherited={"PATH": "/usr/bin", "HOME": "/home/person"},
            requested=(),
            policy=EnvironmentPolicy(include_home_paths=True),
        )

        self.assertNotIn("HOME", dict(without_home.variables))
        self.assertEqual(dict(with_home.variables)["HOME"], "/home/person")

    def test_redacted_environment_never_exposes_values(self):
        redacted = redact_environment(
            (("PATH", "/usr/bin"), ("CI", "sensitive-looking-value"))
        )

        self.assertEqual(
            redacted,
            (("PATH", REDACTED_VALUE), ("CI", REDACTED_VALUE)),
        )

    def test_output_order_does_not_depend_on_input_mapping_order(self):
        first = construct_environment(
            platform="posix",
            inherited={"PATH": "/bin", "LANG": "C", "TERM": "xterm"},
            requested=(),
        )
        second = construct_environment(
            platform="posix",
            inherited={"TERM": "xterm", "LANG": "C", "PATH": "/bin"},
            requested=(),
        )

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
