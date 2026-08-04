from __future__ import annotations

import subprocess
import unittest
from pathlib import PureWindowsPath

from truecoder.execution.backends.windows_native import (
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
    JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION,
    JOB_OBJECT_LIMIT_JOB_MEMORY,
    JOB_OBJECT_LIMIT_JOB_TIME,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    build_job_limit_flags,
    creation_flags,
)
from truecoder.execution.backends.windows_plan import (
    CREATE_SUSPENDED_SENTINEL,
    WindowsJobLimits,
    WindowsLaunchPlan,
    build_command_line,
    build_command_shell_argv,
    build_powershell_argv,
    job_limits_from,
    normalize_exit_code,
    normalize_start_error,
    quote_argument,
    quote_powershell_literal,
)
from truecoder.execution.models import ExecutionLimits


class QuotingTests(unittest.TestCase):
    def test_simple_arguments_are_not_quoted(self):
        self.assertEqual(quote_argument("pytest"), "pytest")
        self.assertEqual(quote_argument("-q"), "-q")

    def test_arguments_with_spaces_are_quoted(self):
        self.assertEqual(
            quote_argument(r"C:\Program Files\py.exe"),
            r'"C:\Program Files\py.exe"',
        )

    def test_embedded_quotes_are_escaped(self):
        self.assertEqual(quote_argument('say "hi"'), r'"say \"hi\""')

    def test_backslashes_are_left_alone_when_no_quoting_is_needed(self):
        self.assertEqual(quote_argument("ends\\"), "ends\\")
        self.assertEqual(quote_argument("a\\\\b"), "a\\\\b")

    def test_trailing_backslashes_are_doubled_inside_a_quoted_argument(self):
        self.assertEqual(quote_argument("with space\\"), '"with space\\\\"')
        self.assertEqual(quote_argument("two spaces\\\\"), '"two spaces\\\\\\\\"')

    def test_an_empty_argument_survives_as_an_empty_quoted_string(self):
        self.assertEqual(quote_argument(""), '""')

    def test_command_line_round_trips_through_the_windows_parser(self):
        argv = (
            r"C:\Program Files\py.exe",
            "-c",
            'print("hi")',
            "trailing\\",
            "",
            "plain",
        )
        command_line = build_command_line(argv)

        self.assertEqual(_split_like_windows(command_line), list(argv))

    def test_command_line_rejects_empty_argv(self):
        with self.assertRaises(ValueError):
            build_command_line(())

    def test_command_line_rejects_non_string_entries(self):
        with self.assertRaises(TypeError):
            build_command_line(("cmd", 3))  # type: ignore[arg-type]


class PowerShellQuotingTests(unittest.TestCase):
    def test_single_quotes_are_doubled_in_a_literal(self):
        self.assertEqual(quote_powershell_literal("it's"), "'it''s'")

    def test_powershell_runs_non_interactively_without_a_profile(self):
        argv = build_powershell_argv(r"C:\pwsh.exe", "Get-ChildItem")

        self.assertIn("-NoProfile", argv)
        self.assertIn("-NonInteractive", argv)
        self.assertEqual(argv[-2:], ("-Command", "Get-ChildItem"))

    def test_command_shell_uses_the_non_interactive_switches(self):
        argv = build_command_shell_argv(r"C:\cmd.exe", "dir")

        self.assertEqual(argv, (r"C:\cmd.exe", "/d", "/s", "/c", "dir"))

    def test_an_empty_script_is_refused_for_both_shells(self):
        with self.assertRaises(ValueError):
            build_powershell_argv(r"C:\pwsh.exe", "   ")
        with self.assertRaises(ValueError):
            build_command_shell_argv(r"C:\cmd.exe", "")


class ErrorNormalizationTests(unittest.TestCase):
    def test_known_win32_start_errors_map_to_stable_reasons(self):
        self.assertEqual(normalize_start_error(2), "executable-not-found")
        self.assertEqual(normalize_start_error(3), "working-directory-not-found")
        self.assertEqual(normalize_start_error(5), "permission-denied")
        self.assertEqual(normalize_start_error(193), "not-an-executable")

    def test_an_unknown_error_is_not_invented(self):
        self.assertEqual(normalize_start_error(31337), "process-creation-failed")

    def test_a_normal_exit_code_is_preserved(self):
        self.assertEqual(normalize_exit_code(0), (0, None))
        self.assertEqual(normalize_exit_code(1), (1, None))

    def test_an_ntstatus_exit_becomes_a_signed_code_with_a_reason(self):
        code, detail = normalize_exit_code(0xC0000005)

        self.assertLess(code, 0)
        self.assertEqual(detail, "access-violation")

    def test_a_job_termination_is_distinguished_from_a_crash(self):
        _, detail = normalize_exit_code(0xC000013A)

        self.assertEqual(detail, "control-c-exit")

    def test_exit_code_rejects_non_integers(self):
        with self.assertRaises(TypeError):
            normalize_exit_code(True)


class JobLimitTests(unittest.TestCase):
    def test_kill_on_job_close_is_always_requested(self):
        flags = build_job_limit_flags(
            WindowsJobLimits(memory_bytes=None, cpu_seconds=None, max_processes=None)
        )

        self.assertTrue(flags.flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        self.assertTrue(flags.flags & JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION)

    def test_only_requested_limits_set_their_flag(self):
        flags = build_job_limit_flags(
            WindowsJobLimits(memory_bytes=None, cpu_seconds=None, max_processes=None)
        )

        self.assertFalse(flags.flags & JOB_OBJECT_LIMIT_JOB_MEMORY)
        self.assertFalse(flags.flags & JOB_OBJECT_LIMIT_ACTIVE_PROCESS)
        self.assertFalse(flags.flags & JOB_OBJECT_LIMIT_JOB_TIME)

    def test_every_requested_limit_sets_its_flag_and_value(self):
        flags = build_job_limit_flags(
            WindowsJobLimits(
                memory_bytes=512 * 1024 * 1024,
                cpu_seconds=2.5,
                max_processes=16,
            )
        )

        self.assertTrue(flags.flags & JOB_OBJECT_LIMIT_JOB_MEMORY)
        self.assertTrue(flags.flags & JOB_OBJECT_LIMIT_ACTIVE_PROCESS)
        self.assertTrue(flags.flags & JOB_OBJECT_LIMIT_JOB_TIME)
        self.assertEqual(flags.job_memory_limit, 512 * 1024 * 1024)
        self.assertEqual(flags.active_process_limit, 16)
        self.assertEqual(flags.job_time_100ns, 25_000_000)

    def test_limits_are_translated_from_the_shared_execution_limits(self):
        limits = job_limits_from(
            ExecutionLimits(
                timeout_seconds=30,
                max_output_bytes=1024,
                max_return_bytes=512,
                memory_bytes=1024,
                cpu_seconds=1,
                max_processes=2,
            )
        )

        self.assertEqual(limits.memory_bytes, 1024)
        self.assertEqual(limits.max_processes, 2)
        self.assertEqual(limits.cpu_100ns_ticks, 10_000_000)

    def test_negative_limits_are_refused(self):
        with self.assertRaises(ValueError):
            WindowsJobLimits(memory_bytes=0, cpu_seconds=None, max_processes=None)
        with self.assertRaises(ValueError):
            WindowsJobLimits(memory_bytes=None, cpu_seconds=-1, max_processes=None)


class CreationFlagTests(unittest.TestCase):
    def test_processes_are_always_created_suspended(self):
        self.assertTrue(creation_flags() & CREATE_SUSPENDED_SENTINEL)

    def test_processes_never_break_away_from_their_job(self):
        breakaway = 0x01000000

        self.assertFalse(creation_flags() & breakaway)


class LaunchPlanTests(unittest.TestCase):
    def plan(self, **overrides) -> WindowsLaunchPlan:
        values = {
            "argv": ("py.exe", "-V"),
            "command_line": "py.exe -V",
            "working_directory": PureWindowsPath(r"C:\repo"),
            "environment": (("PATH", r"C:\Windows"),),
            "limits": WindowsJobLimits(
                memory_bytes=None,
                cpu_seconds=None,
                max_processes=None,
            ),
            "shell_kind": None,
            **overrides,
        }
        return WindowsLaunchPlan(**values)

    def test_environment_block_is_sorted_and_double_terminated(self):
        plan = self.plan(
            environment=(("Z", "last"), ("A", "first")),
        )

        block = plan.environment_block()
        self.assertTrue(block.endswith("\0\0"))
        self.assertLess(block.index("A=first"), block.index("Z=last"))

    def test_an_empty_command_line_is_refused(self):
        with self.assertRaises(ValueError):
            self.plan(command_line="   ")

    def test_an_empty_argv_is_refused(self):
        with self.assertRaises(ValueError):
            self.plan(argv=())


def _split_like_windows(command_line: str) -> list[str]:
    if hasattr(subprocess, "list2cmdline"):
        return _reference_split(command_line)
    raise unittest.SkipTest("no reference splitter available")


def _reference_split(command_line: str) -> list[str]:
    arguments: list[str] = []
    current: list[str] = []
    backslashes = 0
    in_quotes = False
    started = False
    for character in command_line:
        if character == "\\":
            backslashes += 1
            started = True
            continue
        if character == '"':
            current.append("\\" * (backslashes // 2))
            if backslashes % 2:
                current.append('"')
            else:
                in_quotes = not in_quotes
            backslashes = 0
            started = True
            continue
        current.append("\\" * backslashes)
        backslashes = 0
        if character == " " and not in_quotes:
            if started:
                arguments.append("".join(current))
            current = []
            started = False
            continue
        current.append(character)
        started = True
    current.append("\\" * backslashes)
    if started or current:
        arguments.append("".join(current))
    return arguments


if __name__ == "__main__":
    unittest.main()
