from __future__ import annotations

from typing import ClassVar, Final

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from truecoder.providers.keys import MAX_KEY_CHARACTERS
from truecoder.providers.models import DEFAULT_PROVIDER_NAME

WAITING_MESSAGE: Final = "Waiting for you to finish in the browser..."
COPIED_MESSAGE: Final = "Link copied to the clipboard."
BROWSER_OPENED: Final = "A browser tab should have opened."
BROWSER_REFUSED: Final = "No browser could be opened, so copy the link instead."
NOTE_WITH_BROWSER: Final = (
    "A browser tab should have opened; this link works from any browser."
)
NOTE_WITHOUT_BROWSER: Final = "No browser could be opened, so open this link yourself."


class ApiKeyScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(self, provider: str, model: str = "") -> None:
        self.provider = provider
        self.model = model
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="api-key-dialog"):
            yield Static("API key", classes="credential-title")
            yield Static(self._explanation(), classes="credential-body", markup=False)
            yield Input(
                placeholder="Enter your API key",
                password=True,
                max_length=MAX_KEY_CHARACTERS,
                id="api-key-input",
            )
            yield Static(
                "The key is stored privately in your config directory.",
                classes="credential-note",
                markup=False,
            )
            yield Static("enter save   esc skip", classes="credential-help")

    def _explanation(self) -> str:
        named = self.provider != DEFAULT_PROVIDER_NAME
        if self.model and named:
            return f"{self.model} needs a key for {self.provider} before it can answer."
        if self.model:
            return f"{self.model} needs an API key before it can answer."
        if named:
            return f"{self.provider} needs an API key before it can answer."
        return "This provider needs an API key before it can answer."

    def on_mount(self) -> None:
        self.query_one("#api-key-input", Input).focus()

    @on(Input.Submitted, "#api-key-input")
    def submit_key(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AuthorisationScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("c", "copy", "Copy link", show=False),
    ]

    def __init__(self, provider: str, url: str, *, browser_opened: bool) -> None:
        self.provider = provider
        self.url = url
        self.browser_opened = browser_opened
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="authorisation-dialog"):
            yield Static("Sign in", classes="credential-title")
            yield Static(
                f"Authorise TrueCoder with {self.provider}.",
                classes="credential-body",
                markup=False,
            )
            yield Static(
                BROWSER_OPENED if self.browser_opened else BROWSER_REFUSED,
                classes="credential-note",
                id="authorisation-browser",
                markup=False,
            )
            yield Static(self.url, id="authorisation-url", markup=False)
            with Horizontal(id="authorisation-actions"):
                yield Button("Copy link", id="copy-link", variant="primary")
                yield Button("Open again", id="open-again")
            yield Static(
                WAITING_MESSAGE,
                classes="credential-note",
                id="authorisation-status",
                markup=False,
            )
            yield Static("c copy   esc cancel", classes="credential-help")

    def on_mount(self) -> None:
        self.query_one("#copy-link", Button).focus()

    def report(self, message: str) -> None:
        self.query_one("#authorisation-status", Static).update(message)

    @on(Button.Pressed, "#copy-link")
    def copy_link(self) -> None:
        self.app.copy_to_clipboard(self.url)
        self.report(COPIED_MESSAGE)

    def action_copy(self) -> None:
        self.copy_link()

    @on(Button.Pressed, "#open-again")
    def open_again(self) -> None:
        from truecoder.providers.login import open_in_browser

        opened = open_in_browser(self.url)
        self.report(BROWSER_OPENED if opened else BROWSER_REFUSED)

    def action_cancel(self) -> None:
        self.dismiss(False)
