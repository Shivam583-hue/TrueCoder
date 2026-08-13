from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class FullAccessScreen(ModalScreen[bool]):
    """Confirm the app-session approval bypass before enabling it."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="session-small-dialog", id="full-access-dialog"):
            yield Static("Enable Full Access?", classes="session-dialog-title")
            yield Static(
                "TrueCoder will stop asking before policy-permitted file "
                "changes, commands, network requests, memory changes, and "
                "delegated work for this app session.",
                classes="session-delete-copy",
                markup=False,
            )
            yield Static(
                "Hard policy denials, project boundaries, resource limits, "
                "audit records, checkpoints, and Esc cancellation stay active.",
                classes="session-delete-copy",
                markup=False,
            )
            with Horizontal(classes="session-dialog-actions"):
                yield Button("Cancel", id="full-access-cancel")
                yield Button(
                    "Enable Full Access",
                    id="full-access-confirm",
                    variant="warning",
                )

    def on_mount(self) -> None:
        self.query_one("#full-access-cancel", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#full-access-cancel")
    def cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#full-access-confirm")
    def confirm(self) -> None:
        self.dismiss(True)
