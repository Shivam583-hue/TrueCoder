from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from truecoder.execution.approval import ExecutionApprovalDetails
from truecoder.execution.models import ExecutionLifecycleStage

MAX_PREVIEW_LINES: Final = 200
MAX_PREVIEW_LINE_CHARS: Final = 500
TRUNCATION_NOTE: Final = "… earlier output trimmed from this preview"


@dataclass(frozen=True, slots=True)
class StagePresentation:
    state: str
    label: str
    glyph: str
    terminal: bool


_STAGES: Final[dict[str, StagePresentation]] = {
    "requested": StagePresentation("preparing", "Preparing", "◇", False),
    "policy_evaluated": StagePresentation("preparing", "Checking policy", "◇", False),
    "approval_required": StagePresentation(
        "awaiting-approval",
        "Awaiting approval",
        "◇",
        False,
    ),
    "approved": StagePresentation("preparing", "Approved", "◈", False),
    "backend_selected": StagePresentation("preparing", "Selecting backend", "◈", False),
    "starting": StagePresentation("starting", "Starting", "◈", False),
    "started": StagePresentation("running", "Running", "◈", False),
    "terminating": StagePresentation("terminating", "Stopping", "◈", False),
    "completed": StagePresentation("completed", "Completed", "✓", True),
    "failed": StagePresentation("failed", "Failed", "!", True),
    "timed_out": StagePresentation("failed", "Timed out", "!", True),
    "cancelled": StagePresentation("cancelled", "Cancelled", "×", True),
    "denied": StagePresentation("rejected", "Denied by policy", "×", True),
    "limit_exceeded": StagePresentation("failed", "Limit exceeded", "!", True),
    "failed_to_start": StagePresentation("failed", "Never started", "!", True),
}

EXECUTION_CARD_STATES: Final = frozenset(
    presentation.state for presentation in _STAGES.values()
) | {"cancelling"}


def stage_presentation(stage: ExecutionLifecycleStage) -> StagePresentation:
    try:
        return _STAGES[stage]
    except KeyError:
        raise ValueError(f"unknown execution lifecycle stage: {stage!r}") from None


def is_terminal_stage(stage: ExecutionLifecycleStage) -> bool:
    return stage_presentation(stage).terminal


def _byte_label(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.0f} KiB"
    return f"{value} B"


def _access_label(details: ExecutionApprovalDetails) -> str:
    request = details.request
    filesystem = {
        "host": "full host filesystem",
        "workspace-read": "workspace read-only",
        "workspace-write": "workspace read-write",
    }.get(request.filesystem_mode, request.filesystem_mode)
    network = "network allowed" if request.network_access else "network denied"
    return f"{filesystem} · {network}"


def _limits_label(details: ExecutionApprovalDetails) -> str:
    limits = details.effective_limits
    parts = [
        f"{limits.timeout_seconds:g}s timeout",
        f"{_byte_label(limits.max_output_bytes)} output",
    ]
    if limits.memory_bytes is not None:
        parts.append(f"{_byte_label(limits.memory_bytes)} memory")
    if limits.cpu_seconds is not None:
        parts.append(f"{limits.cpu_seconds:g}s cpu")
    if limits.max_processes is not None:
        parts.append(f"{limits.max_processes} processes")
    return " · ".join(parts)


def scope_label(allowed_scopes: tuple[str, ...]) -> str:
    if not allowed_scopes:
        return "no scope available"
    if tuple(allowed_scopes) == ("once",):
        return "this run only"
    readable = {
        "once": "this run only",
        "session": "this session",
        "workspace": "this workspace",
    }
    return ", ".join(readable.get(scope, scope) for scope in allowed_scopes)


def compact_approval_rows(
    details: ExecutionApprovalDetails,
    allowed_scopes: tuple[str, ...] = ("once",),
) -> tuple[tuple[str, str], ...]:
    return (
        ("Command", details.command_display),
        ("Directory", str(details.working_directory)),
        ("Backend", details.backend),
        ("Access", _access_label(details)),
        ("Limits", _limits_label(details)),
        ("Risk", details.risk.value),
        ("Approval", scope_label(allowed_scopes)),
    )


def full_approval_rows(
    details: ExecutionApprovalDetails,
) -> tuple[tuple[str, str], ...]:
    limits = details.effective_limits
    capabilities = details.capabilities

    def optional(value: object, suffix: str = "") -> str:
        return "not requested" if value is None else f"{value}{suffix}"

    return (
        ("Command", details.command_display),
        ("Directory", str(details.working_directory)),
        ("Backend", details.backend),
        ("Risk", details.risk.value),
        ("Mode", details.request.mode),
        ("Shell", details.request.shell_kind),
        (
            "Network",
            "allowed" if details.request.network_access else "denied",
        ),
        ("Filesystem", details.request.filesystem_mode),
        ("Timeout", f"{limits.timeout_seconds}s"),
        ("Termination grace", f"{limits.termination_grace_seconds}s"),
        ("Output limit", f"{limits.max_output_bytes} bytes"),
        ("Return limit", f"{limits.max_return_bytes} bytes"),
        ("Memory limit", optional(limits.memory_bytes, " bytes")),
        ("CPU limit", optional(limits.cpu_seconds, "s")),
        ("Process limit", optional(limits.max_processes)),
        ("Filesystem isolation", capabilities.filesystem_isolation),
        ("Network isolation", capabilities.network_isolation),
        ("Memory enforcement", capabilities.memory_limits),
        ("CPU enforcement", capabilities.cpu_limits),
        ("Process enforcement", capabilities.process_limits),
        ("Timeout enforcement", capabilities.timeout_enforcement),
        ("Cancellation", capabilities.cancellation),
        ("Supported modes", ", ".join(capabilities.supported_execution_modes)),
        (
            "Supported filesystems",
            ", ".join(capabilities.supported_filesystem_modes),
        ),
        ("Supported shells", ", ".join(capabilities.supported_shells) or "none"),
    )


class BoundedPreview:
    def __init__(
        self,
        *,
        max_lines: int = MAX_PREVIEW_LINES,
        max_line_chars: int = MAX_PREVIEW_LINE_CHARS,
    ) -> None:
        if isinstance(max_lines, bool) or not isinstance(max_lines, int):
            raise TypeError("max_lines must be an integer")
        if max_lines <= 0:
            raise ValueError("max_lines must be greater than zero")
        if isinstance(max_line_chars, bool) or not isinstance(max_line_chars, int):
            raise TypeError("max_line_chars must be an integer")
        if max_line_chars <= 0:
            raise ValueError("max_line_chars must be greater than zero")

        self._max_lines = max_lines
        self._max_line_chars = max_line_chars
        self._lines: list[str] = []
        self._partial = ""
        self.trimmed = False

    def append(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not text:
            return

        combined = self._partial + text.replace("\r\n", "\n").replace("\r", "\n")
        *complete, self._partial = combined.split("\n")
        for line in complete:
            self._lines.append(self._clamp(line))
        self._trim()

    def _clamp(self, line: str) -> str:
        if len(line) <= self._max_line_chars:
            return line
        return f"{line[: self._max_line_chars - 1]}…"

    def _trim(self) -> None:
        overflow = len(self._lines) - self._max_lines
        if overflow > 0:
            del self._lines[:overflow]
            self.trimmed = True

    def text(self) -> str:
        lines = list(self._lines)
        if self._partial:
            lines.append(self._clamp(self._partial))
        if not lines:
            return ""
        if self.trimmed:
            return "\n".join([TRUNCATION_NOTE, *lines])
        return "\n".join(lines)

    def clear(self) -> None:
        self._lines.clear()
        self._partial = ""
        self.trimmed = False
