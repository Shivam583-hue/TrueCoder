import tempfile
import unittest
from pathlib import Path

from truecoder.agent.project_instructions import (
    PROJECT_INSTRUCTIONS_MAX_BYTES,
    ProjectInstructionsError,
    discover_instruction_files,
    find_project_root,
    load_project_instructions,
)


class ProjectRootTests(unittest.TestCase):
    def test_finds_nearest_git_root_from_nested_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            outer_repository = Path(temporary_directory).resolve()
            (outer_repository / ".git").mkdir()
            project = outer_repository / "project"
            project.mkdir()
            (project / ".git").mkdir()
            launch_directory = project / "src" / "feature"
            launch_directory.mkdir(parents=True)

            result = find_project_root(launch_directory)

        self.assertEqual(result, project)

    def test_accepts_worktree_style_git_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory).resolve()
            (project / ".git").write_text(
                "gitdir: ../worktrees/project",
                encoding="utf-8",
            )
            launch_directory = project / "src"
            launch_directory.mkdir()

            result = find_project_root(launch_directory)

        self.assertEqual(result, project)

    def test_falls_back_to_launch_directory_without_git_marker(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            launch_directory = Path(temporary_directory).resolve() / "scratch"
            launch_directory.mkdir()

            result = find_project_root(launch_directory)

        self.assertEqual(result, launch_directory)

    def test_rejects_invalid_launch_directories(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            regular_file = root / "file.txt"
            regular_file.write_text("content", encoding="utf-8")

            with self.assertRaises(TypeError):
                find_project_root("not-a-path")  # type: ignore[arg-type]

            for invalid_path in (root / "missing", regular_file):
                with (
                    self.subTest(invalid_path=invalid_path),
                    self.assertRaises(ProjectInstructionsError),
                ):
                    find_project_root(invalid_path)


class ProjectInstructionDiscoveryTests(unittest.TestCase):
    def test_discovers_and_merges_only_the_root_to_launch_chain(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory).resolve()
            launch_directory = project / "src" / "feature"
            launch_directory.mkdir(parents=True)
            sibling = project / "src" / "sibling"
            sibling.mkdir()

            root_instructions = project / "AGENTS.md"
            nested_instructions = launch_directory / "AGENTS.md"
            root_instructions.write_text("Root guidance", encoding="utf-8")
            nested_instructions.write_text("Nested guidance", encoding="utf-8")
            (sibling / "AGENTS.md").write_text(
                "Sibling guidance",
                encoding="utf-8",
            )

            files = discover_instruction_files(
                project_root=project,
                launch_directory=launch_directory,
            )
            content = load_project_instructions(
                project_root=project,
                launch_directory=launch_directory,
            )

        self.assertEqual(files, (root_instructions, nested_instructions))
        self.assertEqual(content, "Root guidance\n\nNested guidance")

    def test_prefers_override_and_falls_through_when_override_is_empty(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory).resolve()
            launch_directory = project / "nested"
            launch_directory.mkdir()

            root_override = project / "AGENTS.override.md"
            root_override.write_text("Root override", encoding="utf-8")
            (project / "AGENTS.md").write_text(
                "Ignored root guidance",
                encoding="utf-8",
            )
            (launch_directory / "AGENTS.override.md").write_text(
                " \n\t",
                encoding="utf-8",
            )
            nested_instructions = launch_directory / "AGENTS.md"
            nested_instructions.write_text("Nested guidance", encoding="utf-8")

            files = discover_instruction_files(
                project_root=project,
                launch_directory=launch_directory,
            )
            content = load_project_instructions(
                project_root=project,
                launch_directory=launch_directory,
            )

        self.assertEqual(files, (root_override, nested_instructions))
        self.assertEqual(content, "Root override\n\nNested guidance")

    def test_skips_empty_files_and_unsupported_fallback_names(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory).resolve()
            (project / "AGENTS.md").write_text("\n  ", encoding="utf-8")
            (project / "TEAM_GUIDE.md").write_text(
                "Unsupported fallback",
                encoding="utf-8",
            )
            (project / ".agents.md").write_text(
                "Unsupported fallback",
                encoding="utf-8",
            )

            files = discover_instruction_files(
                project_root=project,
                launch_directory=project,
            )
            content = load_project_instructions(
                project_root=project,
                launch_directory=project,
            )

        self.assertEqual(files, ())
        self.assertEqual(content, "")

    def test_does_not_read_instructions_above_the_project_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory).resolve()
            project = parent / "project"
            project.mkdir()
            (parent / "AGENTS.md").write_text(
                "Parent guidance",
                encoding="utf-8",
            )

            content = load_project_instructions(
                project_root=project,
                launch_directory=project,
            )

        self.assertEqual(content, "")

    def test_rejects_invalid_utf8_and_non_file_candidates(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory).resolve()
            instructions = project / "AGENTS.md"
            instructions.write_bytes(b"\xff")

            with self.assertRaises(ProjectInstructionsError):
                load_project_instructions(
                    project_root=project,
                    launch_directory=project,
                )

            instructions.unlink()
            instructions.mkdir()

            with self.assertRaises(ProjectInstructionsError):
                load_project_instructions(
                    project_root=project,
                    launch_directory=project,
                )

    def test_rejects_launch_directory_outside_project_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            project = root / "project"
            project.mkdir()
            outside = root / "outside"
            outside.mkdir()

            with self.assertRaises(ProjectInstructionsError):
                load_project_instructions(
                    project_root=project,
                    launch_directory=outside,
                )


class ProjectInstructionLimitTests(unittest.TestCase):
    def test_accepts_content_at_the_exact_byte_limit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory).resolve()
            launch_directory = project / "nested"
            launch_directory.mkdir()
            (project / "AGENTS.md").write_text("abcd", encoding="utf-8")
            (launch_directory / "AGENTS.md").write_text(
                "efgh",
                encoding="utf-8",
            )

            content = load_project_instructions(
                project_root=project,
                launch_directory=launch_directory,
                max_bytes=10,
            )

        self.assertEqual(content, "abcd\n\nefgh")
        self.assertEqual(len(content.encode("utf-8")), 10)

    def test_truncates_the_last_document_and_stops(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory).resolve()
            launch_directory = project / "nested"
            launch_directory.mkdir()
            (project / "AGENTS.md").write_text("abcd", encoding="utf-8")
            (launch_directory / "AGENTS.md").write_text(
                "efgh",
                encoding="utf-8",
            )

            content = load_project_instructions(
                project_root=project,
                launch_directory=launch_directory,
                max_bytes=9,
            )

        self.assertEqual(content, "abcd\n\nefg")
        self.assertEqual(len(content.encode("utf-8")), 9)

    def test_truncates_at_a_valid_utf8_boundary(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory).resolve()
            (project / "AGENTS.md").write_text("ééé", encoding="utf-8")

            content = load_project_instructions(
                project_root=project,
                launch_directory=project,
                max_bytes=5,
            )

        self.assertEqual(content, "éé")
        self.assertEqual(len(content.encode("utf-8")), 4)

    def test_stops_when_the_separator_and_more_content_do_not_fit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory).resolve()
            launch_directory = project / "nested"
            launch_directory.mkdir()
            (project / "AGENTS.md").write_text("abcd", encoding="utf-8")
            (launch_directory / "AGENTS.md").write_text(
                "more",
                encoding="utf-8",
            )

            content = load_project_instructions(
                project_root=project,
                launch_directory=launch_directory,
                max_bytes=5,
            )

        self.assertEqual(content, "abcd")

    def test_uses_the_32_kib_default(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory).resolve()
            (project / "AGENTS.md").write_text(
                "x" * (PROJECT_INSTRUCTIONS_MAX_BYTES + 1),
                encoding="utf-8",
            )

            content = load_project_instructions(
                project_root=project,
                launch_directory=project,
            )

        self.assertEqual(
            len(content.encode("utf-8")),
            PROJECT_INSTRUCTIONS_MAX_BYTES,
        )

    def test_rejects_invalid_size_limits(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project = Path(temporary_directory).resolve()

            for invalid_limit, error_type in (
                (True, TypeError),
                ("10", TypeError),
                (0, ValueError),
                (-1, ValueError),
            ):
                with (
                    self.subTest(invalid_limit=invalid_limit),
                    self.assertRaises(error_type),
                ):
                    load_project_instructions(
                        project_root=project,
                        launch_directory=project,
                        max_bytes=invalid_limit,  # type: ignore[arg-type]
                    )


if __name__ == "__main__":
    unittest.main()
