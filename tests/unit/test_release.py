from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from truecoder.version import SOURCE_VERSION

ROOT = Path(__file__).resolve().parents[2]


class ReleaseMetadataTests(unittest.TestCase):
    def test_runtime_source_remains_python_310_compatible(self):
        for path in (ROOT / "src" / "truecoder").rglob("*.py"):
            with self.subTest(source=path.relative_to(ROOT)):
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path), feature_version=(3, 10))
                imports_utc_directly = any(
                    isinstance(node, ast.ImportFrom)
                    and node.module == "datetime"
                    and any(name.name == "UTC" for name in node.names)
                    for node in ast.walk(tree)
                )
                self.assertFalse(imports_utc_directly)

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
