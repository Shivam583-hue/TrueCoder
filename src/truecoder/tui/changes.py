from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from truecoder.checkpoint import WorkspaceChanges
from truecoder.tui.widgets import render_diff

HEADER_STYLE = "#4da3ff bold"
PATH_STYLE = "#e9b96e bold"
MUTED_STYLE = "#707070"
UNTRACKED_STYLE = "#a2a2a2"
MAX_RENDERED_LINES = 2000


def render_changes(changes: WorkspaceChanges) -> Text:
    text = Text()

    if changes.is_empty:
        text.append("Nothing changed since ", style=MUTED_STYLE)
        text.append(changes.label, style=PATH_STYLE)
        text.append(".", style=MUTED_STYLE)
        return text

    rendered = 0
    for index, change in enumerate(changes.changes):
        if index:
            text.append("\n")
        text.append(f"{change.path}", style=PATH_STYLE)
        text.append(f"    {change.summary}\n", style=MUTED_STYLE)

        if change.diff is None or change.diff.is_empty:
            continue
        if rendered >= MAX_RENDERED_LINES:
            text.append("  diff not shown, too much changed\n", style=MUTED_STYLE)
            continue

        body = render_diff(change.diff)
        rendered += body.plain.count("\n")
        text.append(body)

    if changes.truncated:
        text.append(
            "\nMore files changed than are shown here.\n",
            style="#f2a33a",
        )

    return text


class WorkspaceChangesScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(
        self,
        changes: WorkspaceChanges | None,
        *,
        unavailable_reason: str | None = None,
    ) -> None:
        self.changes = changes
        self.unavailable_reason = unavailable_reason
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="changes-dialog"):
            yield Static("Changes this turn", classes="session-dialog-title")

            if self.unavailable_reason is not None:
                yield Static(
                    self.unavailable_reason,
                    id="changes-unavailable",
                    markup=False,
                )
                return

            if self.changes is None:
                yield Static(
                    "No checkpoint has been captured yet.",
                    id="changes-empty",
                    markup=False,
                )
                return

            yield Static(
                self.changes.summary,
                id="changes-summary",
                markup=False,
            )
            with VerticalScroll(id="changes-body"):
                yield Static(
                    render_changes(self.changes),
                    id="changes-content",
                    markup=False,
                )
            yield Static(
                "escape closes    ctrl+r restores a checkpoint",
                classes="session-dialog-help",
                markup=False,
            )

    def action_cancel(self) -> None:
        self.dismiss(None)
