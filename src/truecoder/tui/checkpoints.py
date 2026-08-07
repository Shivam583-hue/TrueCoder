from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView, Static

from truecoder.checkpoint import Checkpoint

MAX_PREVIEW_PATHS = 8


def describe_moment(checkpoint: Checkpoint) -> str:
    moment = checkpoint.moment
    if moment is None:
        return "unknown time"
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def describe_removals(paths: tuple[str, ...]) -> str:
    if not paths:
        return "No tracked files will be removed."

    shown = list(paths[:MAX_PREVIEW_PATHS])
    remaining = len(paths) - len(shown)
    listed = "\n".join(f"  {path}" for path in shown)
    if remaining > 0:
        listed += f"\n  and {remaining} more"
    return f"{len(paths)} tracked file(s) will be removed:\n{listed}"


class CheckpointListItem(ListItem):
    def __init__(self, checkpoint: Checkpoint, *, position: int) -> None:
        self.checkpoint = checkpoint
        self.position = position
        super().__init__()

    def compose(self) -> ComposeResult:
        marker = "●" if self.position == 0 else " "
        yield Label(
            f"{marker}  {self.checkpoint.label}\n   {describe_moment(self.checkpoint)}",
            markup=False,
        )


class CheckpointBrowserScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(
        self,
        checkpoints: tuple[Checkpoint, ...],
        *,
        unavailable_reason: str | None = None,
    ) -> None:
        self.checkpoints = checkpoints
        self.unavailable_reason = unavailable_reason
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="checkpoint-dialog"):
            yield Static("Checkpoints", classes="session-dialog-title")

            if self.unavailable_reason is not None:
                yield Static(
                    self.unavailable_reason,
                    id="checkpoint-unavailable",
                    markup=False,
                )
                return

            if not self.checkpoints:
                yield Static(
                    "No checkpoints yet. One is captured before each turn.",
                    id="checkpoint-empty",
                    markup=False,
                )
                return

            yield ListView(
                *(
                    CheckpointListItem(checkpoint, position=position)
                    for position, checkpoint in enumerate(self.checkpoints)
                ),
                id="checkpoint-list",
            )
            yield Static(
                "enter restores the selected checkpoint    escape closes",
                classes="session-dialog-help",
                markup=False,
            )

    def on_mount(self) -> None:
        try:
            self.query_one("#checkpoint-list", ListView).focus()
        except Exception:  # noqa: BLE001 - an empty browser has no list to focus
            return

    @on(ListView.Selected, "#checkpoint-list")
    def select_checkpoint(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, CheckpointListItem):
            self.dismiss(item.checkpoint.checkpoint_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RestoreCheckpointScreen(ModalScreen[bool]):
    def __init__(self, checkpoint: Checkpoint, removals: tuple[str, ...]) -> None:
        self.checkpoint = checkpoint
        self.removals = removals
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(classes="session-small-dialog"):
            yield Static("Restore checkpoint?", classes="session-dialog-title")
            yield Static(
                f'The workspace will return to "{self.checkpoint.label}" '
                f"({describe_moment(self.checkpoint)}).",
                classes="session-delete-copy",
                markup=False,
            )
            yield Static(
                describe_removals(self.removals),
                id="checkpoint-removals",
                markup=False,
            )
            yield Static(
                "A checkpoint of the current state is captured first, so this "
                "can be undone.",
                id="checkpoint-safety",
                markup=False,
            )
            with Horizontal(classes="session-dialog-actions"):
                yield Button("Cancel", id="checkpoint-restore-cancel")
                yield Button(
                    "Restore",
                    id="checkpoint-restore-confirm",
                    variant="error",
                )

    def on_mount(self) -> None:
        self.query_one("#checkpoint-restore-cancel", Button).focus()

    @on(Button.Pressed, "#checkpoint-restore-cancel")
    def cancel_restore(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#checkpoint-restore-confirm")
    def confirm_restore(self) -> None:
        self.dismiss(True)
