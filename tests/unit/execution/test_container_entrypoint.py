from __future__ import annotations

from tests.helpers.platforms import skip_module_on_windows

skip_module_on_windows('POSIX process group signalling')

import importlib.util
import signal
import unittest
from pathlib import Path
from unittest.mock import patch

ENTRYPOINT_PATH = (
    Path(__file__).resolve().parents[3] / "container" / "entrypoint.py"
)
SPEC = importlib.util.spec_from_file_location(
    "truecoder_container_entrypoint",
    ENTRYPOINT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
ENTRYPOINT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENTRYPOINT)


class ContainerEntrypointTests(unittest.TestCase):
    def tearDown(self) -> None:
        ENTRYPOINT._child_process_group = None
        ENTRYPOINT._terminating = False

    def test_cpu_termination_targets_the_child_group_not_pid_one(self):
        with patch.object(ENTRYPOINT.os, "killpg") as killpg:
            ENTRYPOINT._terminate_group(42)

        killpg.assert_called_once_with(42, signal.SIGKILL)

    def test_forwarding_targets_the_owned_child_group(self):
        ENTRYPOINT._child_process_group = 77

        with patch.object(ENTRYPOINT.os, "killpg") as killpg:
            ENTRYPOINT._forward(signal.SIGTERM, None)

        killpg.assert_called_once_with(77, signal.SIGTERM)

    def test_forwarding_before_child_registration_is_safe(self):
        with patch.object(ENTRYPOINT.os, "killpg") as killpg:
            ENTRYPOINT._forward(signal.SIGTERM, None)

        killpg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
