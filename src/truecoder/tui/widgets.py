from __future__ import annotations

from datetime import datetime
from time import monotonic

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Markdown, Static, TextArea

from truecoder.client.response import TokenUsage


ASCII_LOGO = (
    "█████ ████  █   █ █████  ███   ███  ████  █████ ████\n"
    " ░█░░░█░░░█ █░  █░█░░░░░█ ░░░ █ ░░█ █░░░█ █░░░░░█░░░█\n"
    "  █░░░████░░█░░ █░████░░█░ ░░░█░ ░█░█░░░█░████░░████░░\n"
    "  █░░ █░░█░ █░░ █░█░░░░ █░░   █░░ █░█░░ █░█░░░░ █░░█░ ░\n"
    "  █░░ █░░░█░ ███ ░█████░ ███   ███ ░████ ░█████░█░░░█░\n"
    "   ░░  ░░  ░  ░░░ ░░░░░░  ░░░   ░░░ ░░░░░ ░░░░░░ ░░  ░\n"
    "    ░   ░   ░  ░░░  ░░░░░  ░░░   ░░░  ░░░░  ░░░░░ ░   ░"
)


class PromptInput(TextArea):
    """Multiline prompt input with chat-style submission."""

    BINDINGS = [
        Binding("enter", "submit", show=False, priority=True),
        Binding("ctrl+enter", "submit", show=False, priority=True),
        Binding("shift+enter", "newline", show=False, priority=True),
    ]

    class Submitted(Message):
        def __init__(self, prompt_input: PromptInput, value: str) -> None:
            self.prompt_input = prompt_input
            self.value = value
            super().__init__()

        @property
        def control(self) -> PromptInput:
            return self.prompt_input

    def action_submit(self) -> None:
        value = self.text.strip()
        if value:
            self.post_message(self.Submitted(self, value))

    def action_newline(self) -> None:
        self.insert("\n")

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is self:
            line_count = self.text.count("\n") + 1
            self.styles.height = min(8, max(3, line_count + 2))


class TopBar(Horizontal):
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        super().__init__(id="topbar")

    def compose(self) -> ComposeResult:
        yield Static("◆", id="brand-mark", markup=False)
        yield Static("TRUECODER", id="brand-name", markup=False)
        yield Static("CHAT", id="view-label", markup=False)
        yield Static("", classes="bar-spacer")
        yield Static(self.model_name, id="model-name", markup=False)


class EmptyState(Vertical):
    def compose(self) -> ComposeResult:
        yield Static(ASCII_LOGO, id="ascii-logo", markup=False)
        yield Static("TRUECODER", id="compact-logo", markup=False)


class ChatMessage(Vertical):
    """A single user or assistant message in the transcript."""

    def __init__(self, role: str, content: str = "") -> None:
        self.role = role
        self.content_text = content
        self.started_at = monotonic()
        classes = f"chat-message {role}"
        super().__init__(classes=classes)

    def compose(self) -> ComposeResult:
        initial_content = self.content_text
        if self.role == "assistant" and not initial_content:
            initial_content = "_Thinking…_"

        if self.role == "user":
            yield Markdown(initial_content, classes="message-body")
            return

        with Horizontal(classes="message-header"):
            yield Static("◇", classes="role-mark", markup=False)
            yield Static("TRUECODER", classes="role-label", markup=False)
            yield Static("", classes="header-spacer")
            yield Static(
                datetime.now().strftime("%H:%M"),
                classes="message-state",
                markup=False,
            )

        yield Markdown(initial_content, classes="message-body")
        yield Static("", classes="message-footer", markup=False)

    async def append_delta(self, delta: str) -> None:
        if not self.content_text:
            self.query_one(".message-state", Static).update("● streaming")
        self.content_text += delta
        await self.query_one(".message-body", Markdown).update(self.content_text)

    def finish(
        self,
        usage: TokenUsage | None,
        finish_reason: str | None = None,
    ) -> None:
        elapsed = monotonic() - self.started_at
        self.query_one(".message-state", Static).update("done")

        details = [f"{elapsed:.1f}s"]
        if usage is not None:
            details.append(f"{usage.completion_tokens} output tokens")
        if finish_reason and finish_reason != "stop":
            details.append(finish_reason)
        self.query_one(".message-footer", Static).update("  ·  ".join(details))

    async def show_error(self, error: str) -> None:
        self.add_class("error")
        self.query_one(".message-state", Static).update("error")
        safe_error = error.replace("\\", "\\\\").replace("`", "\\`")
        self.content_text = f"**Request failed**\n\n{safe_error}"
        await self.query_one(".message-body", Markdown).update(self.content_text)
        self.query_one(".message-footer", Static).update(
            "Check your connection or API configuration, then try again."
        )

    async def show_cancelled(self) -> None:
        self.add_class("cancelled")
        self.query_one(".message-state", Static).update("stopped")
        if not self.content_text:
            self.content_text = "_Generation stopped._"
            await self.query_one(".message-body", Markdown).update(self.content_text)


class StatusBar(Horizontal):
    def __init__(self) -> None:
        super().__init__(id="statusbar")

    def compose(self) -> ComposeResult:
        yield Static("", classes="bar-spacer")
        yield Static(
            "Ctrl+L  new chat    Esc  stop    Ctrl+Q  quit",
            id="shortcut-hint",
            markup=False,
        )
