from __future__ import annotations

import os
import signal
import sys
import time

ENTRYPOINT_VERSION = "1"
CPU_BUDGET_VARIABLE = "TRUECODER_CPU_SECONDS"
CPU_EXCEEDED_EXIT_CODE = 253
CPU_MARKER_PATH = "/run/truecoder.cpu-exceeded"
CPU_STAT_PATH = "/sys/fs/cgroup/cpu.stat"
POLL_SECONDS = 0.1

_terminating = False


def main(argv: list[str]) -> int:
    if not argv:
        print("truecoder entrypoint requires a command", file=sys.stderr)
        return 64

    if argv[0] == "--version":
        print(ENTRYPOINT_VERSION)
        return 0

    budget = _cpu_budget()
    child = os.fork()
    if child == 0:
        os.setpgid(0, 0)
        try:
            os.execvp(argv[0], argv)
        except OSError as error:
            print(f"exec failed: {error.strerror}", file=sys.stderr)
            os._exit(127)

    try:
        os.setpgid(child, child)
    except OSError:
        pass

    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    return _supervise(child, budget)


def _forward(signal_number: int, _frame) -> None:
    global _terminating
    _terminating = True
    with _suppressed():
        os.killpg(os.getpgid(0), signal_number)


def _supervise(child: int, budget: float | None) -> int:
    exceeded = False
    while True:
        pid, status = os.waitpid(-1, os.WNOHANG)
        if pid == child:
            if exceeded:
                return CPU_EXCEEDED_EXIT_CODE
            return _exit_code(status)
        if pid == 0:
            if budget is not None and not exceeded and _cpu_seconds() > budget:
                exceeded = True
                _mark_cpu_exceeded()
                _terminate_group()
            time.sleep(POLL_SECONDS)


def _exit_code(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def _cpu_budget() -> float | None:
    raw = os.environ.get(CPU_BUDGET_VARIABLE)
    if raw is None:
        return None
    try:
        budget = float(raw)
    except ValueError:
        return None
    return budget if budget > 0 else None


def _cpu_seconds() -> float:
    try:
        with open(CPU_STAT_PATH, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("usage_usec "):
                    return int(line.split()[1]) / 1_000_000
    except (OSError, ValueError, IndexError):
        return 0.0
    return 0.0


def _mark_cpu_exceeded() -> None:
    with _suppressed(), open(CPU_MARKER_PATH, "w", encoding="utf-8") as handle:
        handle.write(ENTRYPOINT_VERSION)


def _terminate_group() -> None:
    with _suppressed():
        os.killpg(os.getpgid(0), signal.SIGKILL)


class _suppressed:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
