"""The shipped .env template is copied verbatim, so it must not disagree with the code."""

from __future__ import annotations

import unittest
from pathlib import Path

from truecoder.agent.context import DEFAULT_MAX_INPUT_TOKENS

TEMPLATE = Path(__file__).resolve().parents[3] / ".env.example"


def _values() -> dict[str, str]:
    values = {}
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator:
            values[name.strip()] = value.strip().strip('"')
    return values


class TemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = _values()

    def test_the_template_exists(self):
        self.assertTrue(TEMPLATE.is_file())

    def test_every_required_variable_is_offered(self):
        for name in ("BASE_URL", "API_KEY", "MODEL"):
            self.assertIn(name, self.values)

    def test_no_credential_is_filled_in(self):
        self.assertEqual(self.values["API_KEY"], "")

    def test_the_offered_budget_matches_the_default(self):
        self.assertEqual(
            self.values["MAX_INPUT_TOKENS"],
            str(DEFAULT_MAX_INPUT_TOKENS),
        )


if __name__ == "__main__":
    unittest.main()
