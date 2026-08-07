from typing import Final

from truecoder.execution.models import ExecutionLimits

DEFAULT_EXECUTION_LIMITS: Final = ExecutionLimits(
    timeout_seconds=600,
    max_output_bytes=1024 * 1024,
    max_return_bytes=64 * 1024,
    memory_bytes=None,
    cpu_seconds=None,
    max_processes=None,
    termination_grace_seconds=2,
)
