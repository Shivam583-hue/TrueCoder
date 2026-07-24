import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from truecoder.agent.agent import run
from truecoder.tools.builtin import ReadFileTool


class CompositionRootTests(unittest.TestCase):
    def test_registers_workspace_bound_read_file_tool_at_startup(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory).resolve()

            with (
                patch(
                    "truecoder.agent.agent.Path.cwd",
                    return_value=workspace,
                ),
                patch("truecoder.agent.agent.Agent") as agent_type,
                patch("truecoder.tui.app.TrueCoderApp") as app_type,
            ):
                run()

        tool_registry = agent_type.call_args.kwargs["tool_registry"]
        read_file_tool = tool_registry.get("read_file")

        self.assertIsInstance(read_file_tool, ReadFileTool)
        self.assertEqual(read_file_tool.workspace_root, workspace)
        app_type.assert_called_once_with(agent_type.return_value)
        app_type.return_value.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
