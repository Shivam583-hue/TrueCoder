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
OAUTH_CHOICE: Final = "oauth"
KEY_CHOICE: Final = "key"
DEVICE_CHOICE: Final = "device"
DEVICE_WAITING: Final = "Waiting for you to approve this code..."


def provider_label(provider: str) -> str:
    if not provider or provider == DEFAULT_PROVIDER_NAME:
        return "This provider"
    return provider


class CredentialChoiceScreen(ModalScreen["str | None"]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("left", "focus_previous", "Previous", show=False),
        Binding("right", "focus_next", "Next", show=False),
    ]

    def __init__(self, provider: str, model: str = "", *, device: bool = False) -> None:
        self.provider = provider
        self.model = model
        self.device = device
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="credential-choice-dialog"):
            yield Static("Connect", classes="credential-title")
            yield Static(self._explanation(), classes="credential-body", markup=False)
            with Horizontal(id="credential-choice-actions"):
                yield Button("Browser sign-in", id="choose-oauth", variant="primary")
                if self.device:
                    yield Button("Enter a code", id="choose-device")
                yield Button("API key", id="choose-key")
            yield Static(
                self._method_note(),
                classes="credential-note",
                markup=False,
            )
            yield Static(
                "tab switch   enter choose   esc cancel",
                classes="credential-help",
            )

    def _explanation(self) -> str:
        who = provider_label(self.provider)
        if self.model:
            return f"{self.model} needs {who} connected. Choose how."
        if self.device:
            return f"{who} accepts browser, code, or API-key sign-in. Choose one."
        return f"{who} accepts a browser sign-in or an API key. Choose one."

    def _method_note(self) -> str:
        sign_in = "Browser and code sign-in use" if self.device else "Browser sign-in uses"
        return (
            f"{sign_in} your existing subscription; an API key bills "
            "the account it belongs to."
        )

    def on_mount(self) -> None:
        self.query_one("#choose-oauth", Button).focus()

    @on(Button.Pressed, "#choose-oauth")
    def choose_oauth(self) -> None:
        self.dismiss(OAUTH_CHOICE)

    @on(Button.Pressed, "#choose-key")
    def choose_key(self) -> None:
        self.dismiss(KEY_CHOICE)

    @on(Button.Pressed, "#choose-device")
    def choose_device(self) -> None:
        self.dismiss(DEVICE_CHOICE)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DeviceCodeScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("c", "copy", "Copy code", show=False),
    ]

    def __init__(
        self,
        provider: str,
        user_code: str,
        url: str,
        *,
        browser_opened: bool = False,
    ) -> None:
        self.provider = provider
        self.user_code = user_code
        self.url = url
        self.browser_opened = browser_opened
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="device-code-dialog"):
            yield Static("Enter a code", classes="credential-title")
            yield Static(
                f"Open this page on any device and enter the code to "
                f"authorise {self.provider}.",
                classes="credential-body",
                markup=False,
            )
            yield Static(self.user_code, id="device-user-code", markup=False)
            yield Static(self.url, id="device-url", markup=False)
            with Horizontal(id="device-actions"):
                yield Button("Copy code", id="copy-code", variant="primary")
                yield Button("Copy link", id="copy-device-link")
            yield Static(
                DEVICE_WAITING,
                classes="credential-note",
                id="device-status",
                markup=False,
            )
            yield Static("c copy   esc cancel", classes="credential-help")

    def on_mount(self) -> None:
        self.query_one("#copy-code", Button).focus()

    def report(self, message: str) -> None:
        self.query_one("#device-status", Static).update(message)

    @on(Button.Pressed, "#copy-code")
    def copy_code(self) -> None:
        self.app.copy_to_clipboard(self.user_code)
        self.report(f"Code {self.user_code} copied to the clipboard.")

    @on(Button.Pressed, "#copy-device-link")
    def copy_link(self) -> None:
        self.app.copy_to_clipboard(self.url)
        self.report(COPIED_MESSAGE)

    def action_copy(self) -> None:
        self.copy_code()

    def action_cancel(self) -> None:
        self.dismiss(False)


class ApiKeyScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(
        self,
        provider: str,
        model: str = "",
        reason: str = "",
        *,
        browser_sign_in: bool = True,
    ) -> None:
        self.provider = provider
        self.model = model
        self.reason = reason
        self.browser_sign_in = browser_sign_in
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical(id="api-key-dialog"):
            yield Static("API key", classes="credential-title")
            yield Static(self._explanation(), classes="credential-body", markup=False)
            if not self.browser_sign_in:
                yield Static(
                    self._only_way_in(),
                    classes="credential-body",
                    markup=False,
                )
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

    def _only_way_in(self) -> str:
        return (
            f"{provider_label(self.provider)} has no browser sign-in configured, "
            "so a key is the only way in. Add an oauth block to providers.json "
            "to be offered both."
        )

    def _explanation(self) -> str:
        if self.reason:
            return self.reason
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
