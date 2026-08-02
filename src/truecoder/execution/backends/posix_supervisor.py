from __future__ import annotations

import argparse
import ctypes
import errno
import os
import selectors
import signal
import sys
import time

from .posix_cgroup import (
    attach_current_process,
    limit_reason,
)
from .posix_limits import apply_rlimit_settings, build_rlimit_settings
from .posix_plan import PosixLaunchPlan, plan_from_payload
from .posix_protocol import (
    PosixFrame,
    read_frame_fd,
    write_frame_fd,
)

_terminate_requested = False
_POLL_SECONDS = 0.05
_FALLBACK_GRACE_SECONDS = 0.5
_MAX_ERROR_CHARS = 2048


def main() -> int:
    arguments = _parse_arguments()
    return run_supervisor(
        config_fd=arguments.config_fd,
        gate_fd=arguments.gate_fd,
        lifetime_fd=arguments.lifetime_fd,
        status_fd=arguments.status_fd,
        control_fd=arguments.control_fd,
    )


def run_supervisor(
    *,
    config_fd: int,
    gate_fd: int,
    lifetime_fd: int,
    status_fd: int,
    control_fd: int,
) -> int:
    project_pid: int | None = None
    project_pgid: int | None = None
    try:
        _install_signal_handlers()
        _enable_subreaper()
        config = read_frame_fd(config_fd)
        if config.type != "CONFIG":
            raise ValueError("first supervisor frame must be CONFIG")
        plan = plan_from_payload(config.payload)
        os.close(config_fd)
        config_fd = -1

        project_pid, project_pgid, child_gate_fd, child_status_fd = (
            _spawn_blocked_project(
                plan,
                close_fds=(
                    gate_fd,
                    lifetime_fd,
                    status_fd,
                    control_fd,
                ),
            )
        )
        child_ready = read_frame_fd(child_status_fd)
        if child_ready.type == "ERROR":
            _send_frame(status_fd, child_ready)
            _terminate_and_reap(
                project_pid,
                project_pgid,
                _FALLBACK_GRACE_SECONDS,
            )
            return 1
        if (
            child_ready.type != "CHILD_READY"
            or child_ready.payload["project_pid"] != project_pid
        ):
            raise ValueError("blocked child returned an invalid ready frame")

        write_frame_fd(
            status_fd,
            "READY",
            {
                "supervisor_pid": os.getpid(),
                "project_pgid": project_pgid,
            },
        )
        if not _wait_for_start(gate_fd, lifetime_fd, control_fd):
            _terminate_and_reap(
                project_pid,
                project_pgid,
                _FALLBACK_GRACE_SECONDS,
            )
            return 0

        write_frame_fd(child_gate_fd, "START", {})
        os.close(child_gate_fd)
        child_gate_fd = -1
        exec_error = _read_exec_result(child_status_fd)
        os.close(child_status_fd)
        child_status_fd = -1
        if exec_error is not None:
            _send_frame(status_fd, exec_error)
            _wait_for_pid(project_pid)
            return 1

        write_frame_fd(
            status_fd,
            "STARTED",
            {"project_pid": project_pid},
        )
        return _monitor_project(
            plan=plan,
            project_pid=project_pid,
            project_pgid=project_pgid,
            lifetime_fd=lifetime_fd,
            control_fd=control_fd,
            status_fd=status_fd,
        )
    except Exception as exc:  # noqa: BLE001
        if project_pid is not None and project_pgid is not None:
            _terminate_and_reap(
                project_pid,
                project_pgid,
                _FALLBACK_GRACE_SECONDS,
            )
        _send_error(
            status_fd,
            operation="supervisor",
            code=type(exc).__name__,
            message=str(exc),
            command_started=False,
        )
        return 1
    finally:
        for fd in (config_fd, gate_fd, lifetime_fd, status_fd, control_fd):
            _close_fd(fd)


def _spawn_blocked_project(
    plan: PosixLaunchPlan,
    *,
    close_fds: tuple[int, ...],
) -> tuple[int, int, int, int]:
    child_gate_read, child_gate_write = os.pipe()
    child_status_read, child_status_write = os.pipe()
    project_pid = os.fork()
    if project_pid == 0:
        _close_fd(child_gate_write)
        _close_fd(child_status_read)
        for fd in close_fds:
            _close_fd(fd)
        _run_blocked_project(
            plan,
            gate_fd=child_gate_read,
            status_fd=child_status_write,
        )
        os._exit(126)

    _close_fd(child_gate_read)
    _close_fd(child_status_write)
    try:
        os.setpgid(project_pid, project_pid)
    except OSError as exc:
        if exc.errno not in {errno.EACCES, errno.ESRCH}:
            raise
    return project_pid, project_pid, child_gate_write, child_status_read


def _run_blocked_project(
    plan: PosixLaunchPlan,
    *,
    gate_fd: int,
    status_fd: int,
) -> None:
    try:
        os.setpgid(0, 0)
        os.chdir(plan.working_directory)
        if plan.cgroup_path is not None:
            attach_current_process(plan.cgroup_path)
        apply_rlimit_settings(build_rlimit_settings(plan.limits))
        write_frame_fd(
            status_fd,
            "CHILD_READY",
            {"project_pid": os.getpid()},
        )
        gate = read_frame_fd(gate_fd)
        if gate.type != "START":
            raise ValueError("project launch gate did not contain START")
        _close_fd(gate_fd)
        os.execvpe(
            plan.argv[0],
            list(plan.argv),
            dict(plan.environment),
        )
    except Exception as exc:  # noqa: BLE001
        _send_error(
            status_fd,
            operation="exec",
            code=type(exc).__name__,
            message=str(exc),
            command_started=False,
        )
    finally:
        _close_fd(gate_fd)
        _close_fd(status_fd)


def _wait_for_start(
    gate_fd: int,
    lifetime_fd: int,
    control_fd: int,
) -> bool:
    with selectors.DefaultSelector() as selector:
        selector.register(gate_fd, selectors.EVENT_READ, "gate")
        selector.register(lifetime_fd, selectors.EVENT_READ, "lifetime")
        selector.register(control_fd, selectors.EVENT_READ, "control")
        while True:
            if _terminate_requested:
                return False
            for key, _events in selector.select(_POLL_SECONDS):
                if key.data == "lifetime":
                    if not os.read(lifetime_fd, 1):
                        return False
                elif key.data == "control":
                    try:
                        frame = read_frame_fd(control_fd)
                    except EOFError:
                        return False
                    if frame.type != "TERMINATE":
                        raise ValueError("invalid pre-start control frame")
                    return False
                else:
                    try:
                        frame = read_frame_fd(gate_fd)
                    except EOFError:
                        return False
                    if frame.type != "START":
                        raise ValueError("launch gate requires START")
                    return True


def _read_exec_result(child_status_fd: int) -> PosixFrame | None:
    try:
        frame = read_frame_fd(child_status_fd)
    except EOFError:
        return None
    if frame.type != "ERROR":
        raise ValueError("blocked child returned an invalid exec frame")
    return frame


def _monitor_project(
    *,
    plan: PosixLaunchPlan,
    project_pid: int,
    project_pgid: int,
    lifetime_fd: int,
    control_fd: int,
    status_fd: int,
) -> int:
    termination_reason: str | None = None
    grace_seconds = plan.limits.termination_grace_seconds
    status: int | None = None
    with selectors.DefaultSelector() as selector:
        selector.register(lifetime_fd, selectors.EVENT_READ, "lifetime")
        selector.register(control_fd, selectors.EVENT_READ, "control")
        while status is None:
            waited_pid, waited_status = os.waitpid(project_pid, os.WNOHANG)
            if waited_pid == project_pid:
                status = waited_status
                break
            if _terminate_requested:
                termination_reason = termination_reason or "shutdown"
            cgroup_reason = _cgroup_limit_reason(plan)
            if cgroup_reason is not None:
                termination_reason = termination_reason or cgroup_reason
            if termination_reason is not None:
                status = _terminate_and_reap(
                    project_pid,
                    project_pgid,
                    grace_seconds,
                )
                break
            for key, _events in selector.select(_POLL_SECONDS):
                if key.data == "lifetime":
                    if not os.read(lifetime_fd, 1):
                        termination_reason = "shutdown"
                else:
                    try:
                        frame = read_frame_fd(control_fd)
                    except EOFError:
                        termination_reason = "shutdown"
                        continue
                    if frame.type != "TERMINATE":
                        raise ValueError("invalid runtime control frame")
                    termination_reason = str(frame.payload["reason"])
                    grace_seconds = float(frame.payload["grace_seconds"])

    assert status is not None
    final_limit_reason = _cgroup_limit_reason(plan)
    termination_reason = termination_reason or final_limit_reason
    _terminate_group_only(project_pgid, min(grace_seconds, 0.25))
    _reap_available_children()
    payload = _exit_payload(status, termination_reason)
    write_frame_fd(status_fd, "EXIT", payload)
    return 0


def _cgroup_limit_reason(plan: PosixLaunchPlan) -> str | None:
    if plan.cgroup_path is None:
        return None
    try:
        from .posix_cgroup import CgroupCounters, PosixCgroup

        cgroup = PosixCgroup(
            path=plan.cgroup_path,
            delegated_root=plan.cgroup_path.parent,
            controllers=tuple(
                plan.cgroup_controllers
            ),
            baseline=CgroupCounters(0, 0, 0),
        )
        return limit_reason(
            cgroup,
            cpu_limit_seconds=plan.limits.cpu_seconds,
        )
    except OSError:
        return None


def _terminate_and_reap(pid: int, pgid: int, grace_seconds: float) -> int:
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        waited_pid, status = os.waitpid(pid, os.WNOHANG)
        if waited_pid == pid:
            _terminate_group_only(pgid, 0)
            _reap_available_children()
            return status
        time.sleep(min(_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    _signal_group(pgid, signal.SIGKILL)
    status = _wait_for_pid(pid)
    _reap_available_children()
    return status


def _terminate_group_only(pgid: int, grace_seconds: float) -> None:
    if not _group_exists(pgid):
        return
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while _group_exists(pgid) and time.monotonic() < deadline:
        _reap_available_children()
        time.sleep(_POLL_SECONDS)
    if _group_exists(pgid):
        _signal_group(pgid, signal.SIGKILL)
    deadline = time.monotonic() + 1.0
    while _group_exists(pgid) and time.monotonic() < deadline:
        _reap_available_children()
        time.sleep(_POLL_SECONDS)


def _wait_for_pid(pid: int) -> int:
    while True:
        try:
            waited_pid, status = os.waitpid(pid, 0)
        except InterruptedError:
            continue
        if waited_pid == pid:
            return status


def _reap_available_children() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _exit_payload(
    status: int,
    termination_reason: str | None,
) -> dict[str, object]:
    if termination_reason is not None:
        return {
            "exit_code": None,
            "signal": os.WTERMSIG(status) if os.WIFSIGNALED(status) else None,
            "native_reason": termination_reason,
        }
    if os.WIFEXITED(status):
        return {
            "exit_code": os.WEXITSTATUS(status),
            "signal": None,
            "native_reason": None,
        }
    signal_number = os.WTERMSIG(status)
    return {
        "exit_code": 128 + signal_number,
        "signal": signal_number,
        "native_reason": f"signal-{signal_number}",
    }


def _signal_group(pgid: int, signal_number: int) -> None:
    try:
        os.killpg(pgid, signal_number)
    except ProcessLookupError:
        pass


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _install_signal_handlers() -> None:
    def request_termination(
        _signal_number: int,
        _frame: object,
    ) -> None:
        global _terminate_requested
        _terminate_requested = True

    signal.signal(signal.SIGTERM, request_termination)
    signal.signal(signal.SIGINT, request_termination)


def _enable_subreaper() -> None:
    if sys.platform != "linux":
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            return
    except (AttributeError, OSError):
        return


def _send_frame(status_fd: int, frame: PosixFrame) -> None:
    try:
        write_frame_fd(status_fd, frame.type, frame.payload)
    except OSError:
        pass


def _send_error(
    status_fd: int,
    *,
    operation: str,
    code: str,
    message: str,
    command_started: bool,
) -> None:
    bounded = message.strip() or code
    if len(bounded) > _MAX_ERROR_CHARS:
        bounded = bounded[: _MAX_ERROR_CHARS - 14] + "...[truncated]"
    try:
        write_frame_fd(
            status_fd,
            "ERROR",
            {
                "operation": operation,
                "code": code,
                "message": bounded,
                "command_started": command_started,
            },
        )
    except (OSError, ValueError):
        pass


def _close_fd(fd: int) -> None:
    if fd < 0:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config-fd", type=int, required=True)
    parser.add_argument("--gate-fd", type=int, required=True)
    parser.add_argument("--lifetime-fd", type=int, required=True)
    parser.add_argument("--status-fd", type=int, required=True)
    parser.add_argument("--control-fd", type=int, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
