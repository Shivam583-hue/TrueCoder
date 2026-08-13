from __future__ import annotations

import re
import unittest
from pathlib import Path

from truecoder.version import SOURCE_VERSION

ROOT = Path(__file__).resolve().parents[2]


class ReleaseMetadataTests(unittest.TestCase):
    def test_source_and_package_versions_agree(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)

        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), SOURCE_VERSION)

    def test_installers_target_this_release(self):
        for name in ("install.sh", "install.ps1"):
            installer = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            with self.subTest(installer=name):
                self.assertIn(SOURCE_VERSION, installer)
                self.assertIn("Shivam583-hue/TrueCoder", installer)
                self.assertIn("releases/download", installer)


if __name__ == "__main__":
    unittest.main()
