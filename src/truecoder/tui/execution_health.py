from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from truecoder.execution.bootstrap import ExecutionHealthReport


def health_lines(report: ExecutionHealthReport) -> tuple[str, ...]:
    if not isinstance(report, ExecutionHealthReport):
        raise TypeError("report must be an ExecutionHealthReport")
    lines = [
        f"Shell      {'available' if report.shell_available else 'unavailable'}",
        f"Audit      {'ready' if report.audit_ready else 'unavailable'}",
        f"Recovery   {'ready' if report.recovery_ready else 'unavailable'}",
    ]
    if report.failure_code is not None:
        lines.append(f"Reason     {report.failure_code}")
    lines.append("")
    lines.append("Backends")
    if not report.backends:
        lines.append("  No backends were discovered")
    for backend in report.backends:
        if backend.registered:
            state = "ready"
        elif backend.discovered:
            state = "unavailable"
        else:
            state = "not discovered"
        lines.append(f"  {backend.name:<10} {state}")
        lines.extend(f"    {reason}" for reason in backend.reasons)
    return tuple(lines)


def health_failure_message(report: ExecutionHealthReport) -> str | None:
    if report.shell_available:
        return None
    if report.failure_code is not None:
        return report.failure_code.replace("_", " ")
    for backend in report.backends:
        if backend.reasons:
            return backend.reasons[0]
    return "no compatible execution backend is available"


class ExecutionHealthScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(self, report: ExecutionHealthReport) -> None:
        if not isinstance(report, ExecutionHealthReport):
            raise TypeError("report must be an ExecutionHealthReport")
        self.report = report
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="execution-health-dialog"):
            yield Static("Execution status", classes="execution-health-title")
            yield Static(
                "\n".join(health_lines(self.report)),
                id="execution-health-details",
                markup=False,
            )
            yield Static("esc close", classes="execution-health-help")

    def action_close(self) -> None:
        self.dismiss(None)
