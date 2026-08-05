from __future__ import annotations

from tests.helpers.platforms import skip_module_on_windows

skip_module_on_windows('cgroup v2')

import unittest
from pathlib import Path

from truecoder.execution.backends.models import CgroupV2Info
from truecoder.execution.backends.posix_cgroup import (
    CgroupIO,
    cleanup_cgroup,
    create_execution_cgroup,
    limit_reason,
)
from truecoder.execution.models import ExecutionLimits

ROOT = Path("/sys/fs/cgroup/test.scope")


class FakeCgroupIO(CgroupIO):
    def __init__(self) -> None:
        self.directories: set[Path] = set()
        self.files: dict[Path, str] = {}

    def make_directory(self, path: Path) -> None:
        if path in self.directories:
            raise FileExistsError(path)
        self.directories.add(path)
        self.files[path / "cpu.stat"] = "usage_usec 10\n"
        self.files[path / "memory.events"] = "oom_kill 0\n"
        self.files[path / "pids.events"] = "max 0\n"
        self.files[path / "cgroup.kill"] = ""

    def write_text(self, path: Path, value: str) -> None:
        self.files[path] = value

    def read_text(self, path: Path) -> str:
        return self.files[path]

    def exists(self, path: Path) -> bool:
        return path in self.files or path in self.directories

    def remove_directory(self, path: Path) -> None:
        self.directories.remove(path)
        self.files = {
            file_path: value
            for file_path, value in self.files.items()
            if file_path.parent != path
        }


def _limits() -> ExecutionLimits:
    return ExecutionLimits(
        timeout_seconds=5,
        max_output_bytes=1024,
        max_return_bytes=512,
        memory_bytes=4096,
        cpu_seconds=0.5,
        max_processes=3,
    )


def _info(**overrides: object) -> CgroupV2Info:
    values: dict[str, object] = {
        "mounted": True,
        "writable": True,
        "controllers": ("cpu", "memory", "pids"),
        "enabled_controllers": ("cpu", "memory", "pids"),
        "delegated_path": ROOT,
    }
    values.update(overrides)
    return CgroupV2Info(**values)  # type: ignore[arg-type]


class PosixCgroupTests(unittest.TestCase):
    def test_creates_tokenized_owned_cgroup_and_writes_hard_controls(self):
        io = FakeCgroupIO()

        cgroup = create_execution_cgroup(
            _info(),
            execution_id="exec_one",
            ownership_token="owner_one",
            limits=_limits(),
            io=io,
        )

        self.assertIsNotNone(cgroup)
        assert cgroup is not None
        self.assertEqual(io.files[cgroup.path / "memory.max"], "4096")
        self.assertEqual(io.files[cgroup.path / "pids.max"], "3")
        self.assertNotIn("exec_one", cgroup.path.name)
        cleanup_cgroup(cgroup, io=io)
        self.assertNotIn(cgroup.path, io.directories)

    def test_uses_only_controllers_discovery_marked_enforced(self):
        cgroup = create_execution_cgroup(
            _info(enabled_controllers=("memory", "pids")),
            execution_id="exec_two",
            ownership_token="owner_two",
            limits=_limits(),
            io=FakeCgroupIO(),
        )

        self.assertIsNotNone(cgroup)
        assert cgroup is not None
        self.assertEqual(cgroup.controllers, ("memory", "pids"))

    def test_limit_reason_uses_counter_deltas(self):
        io = FakeCgroupIO()
        cgroup = create_execution_cgroup(
            _info(),
            execution_id="exec_three",
            ownership_token="owner_three",
            limits=_limits(),
            io=io,
        )
        assert cgroup is not None
        io.files[cgroup.path / "cpu.stat"] = "usage_usec 500011\n"

        self.assertEqual(
            limit_reason(cgroup, cpu_limit_seconds=0.5, io=io),
            "cpu_limit",
        )
        io.files[cgroup.path / "memory.events"] = "oom_kill 1\n"
        self.assertEqual(
            limit_reason(cgroup, cpu_limit_seconds=0.5, io=io),
            "memory_limit",
        )

    def test_unwritable_or_unmounted_cgroup_is_not_used(self):
        for info in (
            None,
            CgroupV2Info(mounted=False, writable=False),
            _info(writable=False),
        ):
            with self.subTest(info=info):
                self.assertIsNone(
                    create_execution_cgroup(
                        info,
                        execution_id="exec_four",
                        ownership_token="owner_four",
                        limits=_limits(),
                        io=FakeCgroupIO(),
                    )
                )


if __name__ == "__main__":
    unittest.main()
