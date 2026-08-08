"""A rooted path must be refused whichever platform's convention wrote it."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from truecoder.workspace import is_workspace_relative, resolve_inside_workspace

ROOTED = (
    "/etc",
    "/etc/passwd",
    "\\Windows",
    "\\\\server\\share",
    "C:/Windows",
    "C:\\Windows",
    "C:Windows",
    "d:/data",
)

RELATIVE = ("servers", "./servers", "a/b/c", ".", "a", "sub\\dir")


class IsWorkspaceRelativeTests(unittest.TestCase):
    def test_a_rooted_path_is_refused_under_either_convention(self):
        for value in ROOTED:
            with self.subTest(value=value):
                self.assertFalse(is_workspace_relative(value))

    def test_an_ordinary_relative_path_is_accepted(self):
        for value in RELATIVE:
            with self.subTest(value=value):
                self.assertTrue(is_workspace_relative(value))

    def test_empty_or_non_text_is_refused(self):
        for value in ("", "   ", None, 3, Path("servers")):
            with self.subTest(value=value):
                self.assertFalse(is_workspace_relative(value))


class ResolveInsideWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def test_the_root_itself_resolves(self):
        self.assertEqual(resolve_inside_workspace(self.root, "."), self.root)

    def test_a_nested_directory_resolves(self):
        (self.root / "servers").mkdir()

        self.assertEqual(
            resolve_inside_workspace(self.root, "servers"),
            self.root / "servers",
        )

    def test_every_rooted_form_is_refused(self):
        for value in ROOTED:
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_inside_workspace(self.root, value)

    def test_a_traversal_escape_is_refused(self):
        for value in ("..", "../elsewhere", "servers/../.."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_inside_workspace(self.root, value)

    def test_the_subject_names_what_was_refused(self):
        with self.assertRaises(ValueError) as caught:
            resolve_inside_workspace(
                self.root, "/etc", subject="hook working directory"
            )

        self.assertIn("hook working directory", str(caught.exception))

    def test_a_non_path_root_is_rejected(self):
        with self.assertRaises(TypeError):
            resolve_inside_workspace("/workspace", "servers")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
