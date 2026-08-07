from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from truecoder.agent.environment import (
    EnvironmentFacts,
    collect_environment,
    describe_environment,
    find_workspace_interpreter,
)
from truecoder.agent.prompts import build_system_prompt


def _facts(**overrides) -> EnvironmentFacts:
    values = {
        "working_directory": "/workspace",
        "operating_system": "Linux 6.19",
        "interpreter": "/usr/bin/python3",
        "interpreter_version": "3.14.3",
    }
    values.update(overrides)
    return EnvironmentFacts(**values)


class WorkspaceInterpreterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        self.addCleanup(self._directory.cleanup)

    def _create(self, relative: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")

    def test_no_virtual_environment_is_reported_as_none(self):
        self.assertIsNone(find_workspace_interpreter(self.root))

    def test_a_dot_venv_is_found_first(self):
        self._create(".venv/bin/python3")
        self._create("venv/bin/python3")

        with patch("platform.system", return_value="Linux"):
            found = find_workspace_interpreter(self.root)

        self.assertEqual(found, str(Path(".venv/bin/python3")))

    def test_a_plain_venv_is_found_when_dot_venv_is_absent(self):
        self._create("venv/bin/python3")

        with patch("platform.system", return_value="Linux"):
            found = find_workspace_interpreter(self.root)

        self.assertEqual(found, str(Path("venv/bin/python3")))

    def test_a_directory_without_an_interpreter_is_not_reported(self):
        (self.root / ".venv").mkdir()

        self.assertIsNone(find_workspace_interpreter(self.root))

    def test_windows_looks_for_the_scripts_interpreter(self):
        self._create(".venv/Scripts/python.exe")

        with patch("platform.system", return_value="Windows"):
            found = find_workspace_interpreter(self.root)

        self.assertEqual(found, str(Path(".venv/Scripts/python.exe")))

    def test_a_non_path_root_is_rejected(self):
        with self.assertRaises(TypeError):
            find_workspace_interpreter("/workspace")  # type: ignore[arg-type]


class CollectEnvironmentTests(unittest.TestCase):
    def test_the_real_machine_is_described_without_blanks(self):
        facts = collect_environment(Path.cwd())

        self.assertTrue(facts.working_directory)
        self.assertTrue(facts.operating_system)
        self.assertTrue(facts.interpreter)
        self.assertTrue(facts.interpreter_version)

    def test_a_non_path_root_is_rejected(self):
        with self.assertRaises(TypeError):
            collect_environment(".")  # type: ignore[arg-type]


class DescribeEnvironmentTests(unittest.TestCase):
    def test_every_fact_reaches_the_description(self):
        described = describe_environment(
            _facts(workspace_interpreter=".venv/bin/python3")
        )

        self.assertIn("<environment>", described)
        self.assertIn("</environment>", described)
        self.assertIn("/workspace", described)
        self.assertIn("Linux 6.19", described)
        self.assertIn("/usr/bin/python3", described)
        self.assertIn("3.14.3", described)
        self.assertIn(".venv/bin/python3", described)

    def test_a_missing_virtual_environment_is_stated_rather_than_omitted(self):
        described = describe_environment(_facts())

        self.assertIn("none found", described)

    def test_empty_facts_are_rejected(self):
        with self.assertRaises(ValueError):
            _facts(working_directory="   ")


class SystemPromptEnvironmentTests(unittest.TestCase):
    def test_the_environment_block_is_included(self):
        prompt = build_system_prompt("", describe_environment(_facts()))

        self.assertIn("<environment>", prompt)
        self.assertIn("gathered at startup", prompt)

    def test_project_instructions_still_follow_the_environment(self):
        prompt = build_system_prompt("Use tabs.", describe_environment(_facts()))

        self.assertLess(
            prompt.index("<environment>"),
            prompt.index("<project_instructions>"),
        )
        self.assertIn("Use tabs.", prompt)

    def test_no_environment_leaves_the_prompt_unchanged(self):
        self.assertEqual(build_system_prompt(""), build_system_prompt("", ""))
        self.assertNotIn("<environment>", build_system_prompt(""))

    def test_a_non_string_environment_is_rejected(self):
        with self.assertRaises(TypeError):
            build_system_prompt("", None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
