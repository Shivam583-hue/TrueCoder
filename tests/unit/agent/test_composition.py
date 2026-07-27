import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from truecoder.agent.agent import run
from truecoder.tools.builtin import ReadFileTool


class CompositionRootTests(unittest.TestCase):
    def test_loads_instructions_once_and_binds_tools_to_project_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory).resolve()
            launch_directory = project_root / "src" / "feature"
            launch_directory.mkdir(parents=True)
            context_builder = Mock()

            with (
                patch(
                    "truecoder.agent.agent.Path.cwd",
                    return_value=launch_directory,
                ),
                patch(
                    "truecoder.agent.agent.find_project_root",
                    return_value=project_root,
                ) as find_root,
                patch(
                    "truecoder.agent.agent.load_project_instructions",
                    return_value="Repository guidance",
                ) as load_instructions,
                patch(
                    "truecoder.agent.agent.ContextBuilder.from_environment",
                    return_value=context_builder,
                ) as build_context,
                patch("truecoder.agent.agent.Agent") as agent_type,
                patch("truecoder.tui.app.TrueCoderApp") as app_type,
            ):
                run()

        tool_registry = agent_type.call_args.kwargs["tool_registry"]
        read_file_tool = tool_registry.get("read_file")

        find_root.assert_called_once_with(launch_directory)
        load_instructions.assert_called_once_with(
            project_root=project_root,
            launch_directory=launch_directory,
        )
        build_context.assert_called_once_with(
            project_instructions="Repository guidance",
        )
        self.assertIsInstance(read_file_tool, ReadFileTool)
        self.assertEqual(read_file_tool.workspace_root, project_root)
        self.assertIs(
            agent_type.call_args.kwargs["context_builder"],
            context_builder,
        )
        app_type.assert_called_once_with(agent_type.return_value)
        app_type.return_value.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
