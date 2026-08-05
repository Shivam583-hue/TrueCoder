from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Final

from .windows_plan import (
    STILL_ACTIVE,
    WindowsJobLimits,
    WindowsLaunchPlan,
    normalize_exit_code,
    normalize_start_error,
)

WINDOWS = sys.platform == "win32"

CREATE_SUSPENDED: Final = 0x00000004
CREATE_NEW_PROCESS_GROUP: Final = 0x00000200
CREATE_UNICODE_ENVIRONMENT: Final = 0x00000400
CREATE_NO_WINDOW: Final = 0x08000000
CREATE_BREAKAWAY_FROM_JOB: Final = 0x01000000

STARTF_USESTDHANDLES: Final = 0x00000100
HANDLE_FLAG_INHERIT: Final = 0x00000001
INFINITE: Final = 0xFFFFFFFF
WAIT_OBJECT_0: Final = 0x00000000
WAIT_TIMEOUT: Final = 0x00000102
WAIT_FAILED: Final = 0xFFFFFFFF
ERROR_BROKEN_PIPE: Final = 109
ERROR_NO_DATA: Final = 232

JOB_OBJECT_LIMIT_ACTIVE_PROCESS: Final = 0x00000008
JOB_OBJECT_LIMIT_JOB_MEMORY: Final = 0x00000200
JOB_OBJECT_LIMIT_JOB_TIME: Final = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION: Final = 0x00000400

JobObjectExtendedLimitInformation: Final = 9
JobObjectBasicAccountingInformation: Final = 1


class WindowsNativeError(RuntimeError):
    def __init__(self, operation: str, error_code: int) -> None:
        self.operation = operation
        self.error_code = error_code
        self.reason = normalize_start_error(error_code)
        super().__init__(f"{operation} failed with error {error_code}")


@dataclass(frozen=True, slots=True)
class JobLimitFlags:
    flags: int
    active_process_limit: int
    job_memory_limit: int
    job_time_100ns: int


def build_job_limit_flags(limits: WindowsJobLimits) -> JobLimitFlags:
    if not isinstance(limits, WindowsJobLimits):
        raise TypeError("limits must be WindowsJobLimits")

    flags = JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
    if limits.kill_on_job_close:
        flags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

    active_process_limit = 0
    if limits.max_processes is not None:
        flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        active_process_limit = limits.max_processes

    job_memory_limit = 0
    if limits.memory_bytes is not None:
        flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        job_memory_limit = limits.memory_bytes

    job_time = 0
    ticks = limits.cpu_100ns_ticks
    if ticks is not None:
        flags |= JOB_OBJECT_LIMIT_JOB_TIME
        job_time = ticks

    return JobLimitFlags(
        flags=flags,
        active_process_limit=active_process_limit,
        job_memory_limit=job_memory_limit,
        job_time_100ns=job_time,
    )


def creation_flags() -> int:
    return (
        CREATE_SUSPENDED
        | CREATE_NEW_PROCESS_GROUP
        | CREATE_UNICODE_ENVIRONMENT
        | CREATE_NO_WINDOW
    )


@dataclass(slots=True)
class NativeProcess:
    process_handle: int
    thread_handle: int
    process_id: int
    job_handle: int
    stdout_read: int
    stderr_read: int


if WINDOWS:  # pragma: no cover - exercised only on Windows CI
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", wintypes.LPVOID),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    _kernel32.CreatePipe.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wintypes.DWORD,
    )
    _kernel32.CreatePipe.restype = wintypes.BOOL
    _kernel32.SetHandleInformation.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    _kernel32.SetHandleInformation.restype = wintypes.BOOL
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.CreateProcessW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    )
    _kernel32.CreateProcessW.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
    )
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.PeekNamedPipe.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    )
    _kernel32.PeekNamedPipe.restype = wintypes.BOOL
    _kernel32.ReadFile.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    def _check(result: Any, operation: str) -> Any:
        if not result:
            raise WindowsNativeError(operation, ctypes.get_last_error())
        return result

    def _inheritable_pipe() -> tuple[int, int]:
        attributes = _SECURITY_ATTRIBUTES()
        attributes.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
        attributes.lpSecurityDescriptor = None
        attributes.bInheritHandle = True
        read = wintypes.HANDLE()
        write = wintypes.HANDLE()
        _check(
            _kernel32.CreatePipe(
                ctypes.byref(read),
                ctypes.byref(write),
                ctypes.byref(attributes),
                0,
            ),
            "CreatePipe",
        )
        try:
            _check(
                _kernel32.SetHandleInformation(read, HANDLE_FLAG_INHERIT, 0),
                "SetHandleInformation",
            )
        except WindowsNativeError:
            _kernel32.CloseHandle(read)
            _kernel32.CloseHandle(write)
            raise
        return int(read.value), int(write.value)

    def create_job(limits: WindowsJobLimits) -> int:
        job = _check(_kernel32.CreateJobObjectW(None, None), "CreateJobObjectW")
        flags = build_job_limit_flags(limits)
        information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = flags.flags
        information.BasicLimitInformation.ActiveProcessLimit = (
            flags.active_process_limit
        )
        information.BasicLimitInformation.PerJobUserTimeLimit = flags.job_time_100ns
        information.JobMemoryLimit = flags.job_memory_limit
        try:
            _check(
                _kernel32.SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    ctypes.byref(information),
                    ctypes.sizeof(information),
                ),
                "SetInformationJobObject",
            )
        except WindowsNativeError:
            _kernel32.CloseHandle(job)
            raise
        return int(job)

    def create_suspended(plan: WindowsLaunchPlan) -> NativeProcess:
        job = 0
        stdout_read = 0
        stdout_write = 0
        stderr_read = 0
        stderr_write = 0
        information = _PROCESS_INFORMATION()
        created = False

        try:
            job = create_job(plan.limits)
            stdout_read, stdout_write = _inheritable_pipe()
            stderr_read, stderr_write = _inheritable_pipe()
            startup = _STARTUPINFOW()
            startup.cb = ctypes.sizeof(_STARTUPINFOW)
            startup.dwFlags = STARTF_USESTDHANDLES
            startup.hStdInput = None
            startup.hStdOutput = stdout_write
            startup.hStdError = stderr_write
            command_line = ctypes.create_unicode_buffer(plan.command_line)
            environment = ctypes.create_unicode_buffer(plan.environment_block())
            _check(
                _kernel32.CreateProcessW(
                    None,
                    command_line,
                    None,
                    None,
                    True,
                    creation_flags(),
                    environment,
                    str(plan.working_directory),
                    ctypes.byref(startup),
                    ctypes.byref(information),
                ),
                "CreateProcessW",
            )
            created = True
            _check(
                _kernel32.AssignProcessToJobObject(job, information.hProcess),
                "AssignProcessToJobObject",
            )
            return NativeProcess(
                process_handle=int(information.hProcess),
                thread_handle=int(information.hThread),
                process_id=int(information.dwProcessId),
                job_handle=job,
                stdout_read=stdout_read,
                stderr_read=stderr_read,
            )
        except BaseException:
            if created:
                _kernel32.TerminateProcess(information.hProcess, 1)
                _kernel32.CloseHandle(information.hThread)
                _kernel32.CloseHandle(information.hProcess)
            for handle in (stdout_read, stderr_read, job):
                if handle:
                    _kernel32.CloseHandle(wintypes.HANDLE(handle))
            raise
        finally:
            for handle in (stdout_write, stderr_write):
                if handle:
                    _kernel32.CloseHandle(wintypes.HANDLE(handle))

    def resume(process: NativeProcess) -> None:
        result = _kernel32.ResumeThread(wintypes.HANDLE(process.thread_handle))
        if result == 0xFFFFFFFF:
            raise WindowsNativeError("ResumeThread", ctypes.get_last_error())

    def read_pipe(handle: int, size: int = 65536) -> bytes:
        available = wintypes.DWORD(0)
        ok = _kernel32.PeekNamedPipe(
            wintypes.HANDLE(handle),
            None,
            0,
            None,
            ctypes.byref(available),
            None,
        )
        if not ok:
            error_code = ctypes.get_last_error()
            if error_code in {ERROR_BROKEN_PIPE, ERROR_NO_DATA}:
                return b""
            raise WindowsNativeError("PeekNamedPipe", error_code)
        if available.value == 0:
            return b""
        read_size = min(size, available.value)
        buffer = ctypes.create_string_buffer(read_size)
        read = wintypes.DWORD(0)
        ok = _kernel32.ReadFile(
            wintypes.HANDLE(handle),
            buffer,
            read_size,
            ctypes.byref(read),
            None,
        )
        if not ok:
            error_code = ctypes.get_last_error()
            if error_code in {ERROR_BROKEN_PIPE, ERROR_NO_DATA}:
                return b""
            raise WindowsNativeError("ReadFile", error_code)
        return buffer.raw[: read.value]

    def wait_process(handle: int, timeout_ms: int = INFINITE) -> bool:
        result = _kernel32.WaitForSingleObject(wintypes.HANDLE(handle), timeout_ms)
        if result == WAIT_FAILED:
            raise WindowsNativeError("WaitForSingleObject", ctypes.get_last_error())
        return result == WAIT_OBJECT_0

    def exit_code(handle: int) -> int | None:
        code = wintypes.DWORD(0)
        _check(
            _kernel32.GetExitCodeProcess(
                wintypes.HANDLE(handle),
                ctypes.byref(code),
            ),
            "GetExitCodeProcess",
        )
        if code.value == STILL_ACTIVE:
            return None
        return normalize_exit_code(code.value)[0]

    def terminate_job(job_handle: int) -> None:
        _check(
            _kernel32.TerminateJobObject(wintypes.HANDLE(job_handle), 1),
            "TerminateJobObject",
        )

    def active_process_count(job_handle: int) -> int:
        information = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        returned = wintypes.DWORD(0)
        ok = _kernel32.QueryInformationJobObject(
            wintypes.HANDLE(job_handle),
            JobObjectBasicAccountingInformation,
            ctypes.byref(information),
            ctypes.sizeof(information),
            ctypes.byref(returned),
        )
        _check(ok, "QueryInformationJobObject")
        return int(information.ActiveProcesses)

    def close_handle(handle: int) -> None:
        if handle:
            _check(
                _kernel32.CloseHandle(wintypes.HANDLE(handle)),
                "CloseHandle",
            )

else:

    def _unavailable(*_: object, **__: object):
        raise WindowsNativeError("windows-native-unavailable", 0)

    create_job = _unavailable
    create_suspended = _unavailable
    resume = _unavailable
    read_pipe = _unavailable
    wait_process = _unavailable
    exit_code = _unavailable
    terminate_job = _unavailable
    active_process_count = _unavailable
    close_handle = _unavailable
