from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Button
from textual.worker import Worker, WorkerCancelled, WorkerFailed

from truecoder.agent.agent import Agent
from truecoder.agent.approval import (
    ApprovalIdentity,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalScope,
)
from truecoder.agent.events import AgentEventType
from truecoder.agent.messages import ModelMessage
from truecoder.client.response import TokenUsage
from truecoder.execution.context import workspace_id_for
from truecoder.execution.models import ExecutionLifecycleEvent
from truecoder.planning import Plan
from truecoder.session import SessionError, SessionManager
from truecoder.session.models import SessionRecord
from truecoder.tools.builtin import UpdatePlanTool
from truecoder.tui.audit_view import AuditViewerScreen, audit_row_from
from truecoder.tui.changes import WorkspaceChangesScreen
from truecoder.tui.checkpoints import (
    CheckpointBrowserScreen,
    RestoreCheckpointScreen,
)
from truecoder.tui.commands import (
    help_text,
    looks_like_command,
    parse_command,
    unknown_command_message,
)
from truecoder.tui.credentials import (
    DEVICE_CHOICE,
    OAUTH_CHOICE,
    ApiKeyScreen,
    AuthorisationScreen,
    CredentialChoiceScreen,
    DeviceCodeScreen,
)
from truecoder.tui.execution_health import (
    ExecutionHealthScreen,
    health_failure_message,
)
from truecoder.tui.execution_view import (
    compact_approval_rows,
    full_approval_rows,
    is_terminal_stage,
)
from truecoder.tui.memory import MemoryAction, MemoryBrowserScreen
from truecoder.tui.model_picker import ModelPickerScreen, ProviderInvite
from truecoder.tui.sessions import (
    DeleteSessionScreen,
    RenameSessionScreen,
    SessionAction,
    SessionManagerScreen,
)
from truecoder.tui.widgets import (
    ChatMessage,
    Composer,
    EmptyState,
    ExecutionCard,
    PlanCard,
    PromptInput,
    StatusBar,
    SystemNote,
    ToolCallCard,
)

if TYPE_CHECKING:
    from truecoder.providers.models import ModelInfo

SHUTDOWN_DRAIN_SECONDS: Final = 5.0
MODEL_NOT_CONFIGURED: Final = "model not configured"
SIGN_IN_HEADING: Final = "Sign in"


@dataclass
class _PendingApproval:
    request: ApprovalRequest
    future: asyncio.Future[ApprovalResponse]
    card: ToolCallCard


class ExecutionStageMessage(Message):
    def __init__(self, event: ExecutionLifecycleEvent) -> None:
        self.event = event
        super().__init__()


class ExecutionOutputMessage(Message):
    def __init__(self, execution_id: str, stream: str, text: str) -> None:
        self.execution_id = execution_id
        self.stream = stream
        self.text = text
        super().__init__()


class _AppEventSink:
    def __init__(self, app: App[None]) -> None:
        self._app = app

    async def publish(self, event: ExecutionLifecycleEvent) -> None:
        self._app.post_message(ExecutionStageMessage(event))


class _AppPreviewSink:
    def __init__(self, app: App[None]) -> None:
        self._app = app

    async def publish_bounded(
        self,
        execution_id: str,
        stream: str,
        text: str,
    ) -> None:
        self._app.post_message(ExecutionOutputMessage(execution_id, stream, text))


def _package_version() -> str:
    try:
        return version("truecoder")
    except PackageNotFoundError:
        return "0.1.0"


def _usable(credential) -> bool:
    return credential is not None and credential.is_usable


def _git_branch(workspace: str) -> str | None:
    """Read the nearest repository branch without spawning a subprocess."""
    current = Path(workspace).resolve()
    for directory in (current, *current.parents):
        head_path = directory / ".git" / "HEAD"
        try:
            head = head_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        prefix = "ref: refs/heads/"
        return head[len(prefix) :] if head.startswith(prefix) else None
    return None


class TrueCoderApp(App[None]):
    """A polished terminal chat interface for TrueCoder."""

    CSS_PATH = "styles.tcss"
    TITLE = "TrueCoder"
    ENABLE_COMMAND_PALETTE = False
    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [
        (0, "-narrow"),
        (52, "-compact"),
        (108, "-wide"),
    ]

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+l", "new_chat", "New chat", show=False, priority=True),
        Binding("ctrl+p", "manage_sessions", "Sessions", show=False, priority=True),
        Binding("ctrl+a", "manage_audit", "Audit", show=False, priority=True),
        Binding("ctrl+e", "execution_health", "Execution", show=False, priority=True),
        Binding(
            "ctrl+r", "manage_checkpoints", "Checkpoints", show=False, priority=True
        ),
        Binding("ctrl+d", "review_changes", "Changes", show=False, priority=True),
        Binding("ctrl+n", "manage_memory", "Memory", show=False, priority=True),
        Binding("escape", "cancel_response", "Stop", show=False),
    ]

    def __init__(
        self,
        agent: Agent | None = None,
        *,
        session_manager: SessionManager | None = None,
    ) -> None:
        super().__init__()
        self.agent = agent or Agent()
        self.session_manager = session_manager
        self.agent.approval_handler = self._request_tool_approval
        if session_manager is not None and isinstance(
            session_manager.project_root,
            Path,
        ):
            workspace_id = workspace_id_for(session_manager.project_root)
            self.agent.set_approval_identity_provider(
                lambda: ApprovalIdentity(
                    session_id=session_manager.active_session.session_id,
                    workspace_id=workspace_id,
                )
            )
        self._busy = False
        self._active_worker: Worker[None] | None = None
        self._active_assistant: ChatMessage | None = None
        self._pending_approval: _PendingApproval | None = None
        self._tool_cards: dict[str, ToolCallCard] = {}
        self._execution_cards: dict[str, ExecutionCard] = {}
        self._plan_card: PlanCard | None = None
        self._plan_calls: dict[str, str] = {}
        self._model_name = MODEL_NOT_CONFIGURED
        self.agent.set_execution_sinks(
            event_sink=_AppEventSink(self),
            preview_sink=_AppPreviewSink(self),
        )

    @property
    def messages(self) -> list[ModelMessage]:
        """Expose conversation history for UI state inspection."""
        return self.agent.messages

    def _resolved_model_name(self) -> str:
        from truecoder.providers.models import CredentialError

        try:
            return self.agent.llm_client.settings.model
        except (RuntimeError, CredentialError):
            return MODEL_NOT_CONFIGURED

    def compose(self) -> ComposeResult:
        self._model_name = self._resolved_model_name()
        workspace = os.getcwd()

        with Vertical(id="main"):
            with VerticalScroll(id="transcript"):
                yield EmptyState(id="empty-state")

            yield Composer(self._model_name)

        yield StatusBar(
            workspace,
            branch=_git_branch(workspace),
            version=_package_version(),
            max_input_tokens=self.agent.context_builder.max_input_tokens,
        )

    async def on_mount(self) -> None:
        self.screen.add_class("empty-chat")
        runtime = await self.agent.initialize_execution()
        if runtime is not None:
            failure = health_failure_message(runtime.health)
            self.query_one(StatusBar).set_execution_health(failure)
            if failure is not None:
                self.notify(
                    f"Shell execution unavailable: {failure}.",
                    severity="warning",
                    timeout=8,
                )
        await self._announce_mcp_servers()
        self.query_one(PromptInput).focus()

    async def _announce_mcp_servers(self) -> None:
        manager = self.agent.mcp_manager
        if manager is None:
            return

        await self.agent.initialize_mcp()
        if manager.unavailable_reason is not None:
            self.notify(
                f"Tool servers unavailable: {manager.unavailable_reason}",
                severity="warning",
                timeout=10,
            )
            return

        failures = [status for status in manager.statuses if not status.connected]
        for status in failures:
            self.notify(
                f"Tool server {status.name!r} did not start: {status.reason}",
                severity="warning",
                timeout=10,
            )

    async def on_unmount(self) -> None:
        self._reject_pending_approval_for_shutdown()
        await self._cancel_active_executions(reason="application shutting down")
        await self._drain_active_worker()
        try:
            await self.agent.close()
        finally:
            if self.session_manager is not None:
                self.session_manager.close()

    def _reject_pending_approval_for_shutdown(self) -> None:
        pending = self._pending_approval
        if pending is None or pending.future.done():
            self._clear_pending_approval()
            return
        pending.future.set_result(ApprovalResponse.reject())
        self._clear_pending_approval()

    async def _cancel_active_executions(self, *, reason: str) -> bool:
        service = self._execution_service()
        if service is None:
            return False
        try:
            execution_ids = await service.registry.active_execution_ids()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            return False
        if not execution_ids:
            return False
        for execution_id in execution_ids:
            card = self._execution_cards.get(execution_id)
            if card is not None:
                card.mark_cancelling()
            try:
                await service.cancel(execution_id, reason=reason)
            except Exception:  # noqa: BLE001, S110 - cancellation is a request
                pass
        return True

    async def _drain_active_worker(self) -> None:
        worker = self._active_worker
        if worker is None or not worker.is_running:
            return
        worker.cancel()
        try:
            await asyncio.wait_for(worker.wait(), timeout=SHUTDOWN_DRAIN_SECONDS)
        except (
            TimeoutError,
            WorkerCancelled,
            WorkerFailed,
            asyncio.CancelledError,
        ):
            return

    def _execution_service(self):
        runtime = self.agent.execution_runtime
        return runtime.service if runtime is not None else None

    @on(PromptInput.Submitted)
    async def submit_from_keyboard(self, event: PromptInput.Submitted) -> None:
        if looks_like_command(event.value):
            self.query_one(PromptInput).text = ""
            await self._run_command(event.value)
            return
        await self._submit_prompt(event.value)

    async def _run_command(self, text: str) -> None:
        parsed = parse_command(text)
        if parsed is None:
            self.notify(unknown_command_message(text), severity="warning")
            return

        if parsed.name == "help":
            self.notify(help_text(), timeout=12)
            return

        if parsed.name == "model":
            self.notify(f"Answering with {self._resolved_model_name()}")
            return

        if parsed.name == "models":
            await self._choose_model(refresh=parsed.argument == "refresh")
            return

        if parsed.name == "login":
            self.run_worker(self._collect_credential(), exclusive=False)
            return

        if parsed.name == "logout":
            self._forget_authorisation()
            return

        if parsed.name == "quit":
            await self.action_quit()

    async def _choose_model(self, *, refresh: bool = False) -> None:
        from truecoder.providers.catalog import (
            catalog_problem,
            load_catalog,
            merge_models,
        )
        from truecoder.providers.configuration import selectable_providers
        from truecoder.providers.models import stored_credential

        settings = self.agent.llm_client.settings
        providers = selectable_providers(settings.provider)
        credentials = {
            provider.name: (
                settings.credential
                if provider.name == settings.provider.name
                else stored_credential(provider.name)
            )
            for provider in providers
        }

        slices = await load_catalog(providers, credentials, refresh=refresh)
        models = merge_models(slices)
        invitations = self._invitations(slices, credentials)
        invited = {invitation.provider for invitation in invitations}

        silent = [
            entry.provider.name
            for entry in slices
            if entry.reason is not None and entry.provider.name not in invited
        ]
        if models and silent:
            self.notify(
                f"No models listed for {', '.join(silent)}.",
                severity="warning",
                timeout=8,
            )

        self.push_screen(
            ModelPickerScreen(
                models,
                settings.model,
                active_provider=settings.provider.name,
                invitations=invitations,
                sources={provider.name: provider.label for provider in providers},
                unavailable_reason=catalog_problem(slices),
            ),
            self._apply_model_choice,
        )

    @staticmethod
    def _invitations(slices, credentials) -> tuple[ProviderInvite, ...]:
        missing = [
            entry
            for entry in slices
            if not entry.models and not _usable(credentials.get(entry.provider.name))
        ]
        return tuple(
            ProviderInvite(
                entry.provider.name,
                oauth=entry.provider.oauth is not None,
            )
            for entry in missing
        )

    def _provider_named(self, name: str):
        from truecoder.providers.configuration import selectable_providers

        settings = self.agent.llm_client.settings
        current = settings.provider
        if not name or name == current.name:
            return current
        return next(
            (
                provider
                for provider in selectable_providers(current)
                if provider.name == name
            ),
            current,
        )

    def _apply_model_choice(self, choice: ModelInfo | ProviderInvite | None) -> None:
        if choice is None:
            return
        if isinstance(choice, ProviderInvite):
            self.run_worker(self._accept_invitation(choice), exclusive=False)
            return
        from truecoder.providers.models import stored_credential
        from truecoder.providers.store import StoredSelection, save_selection

        settings = self.agent.llm_client.settings
        provider = self._provider_named(choice.provider)
        if provider.name != settings.provider.name:
            settings.use(provider, stored_credential(provider.name))

        model = choice.identifier
        settings.select_model(model)
        self._model_name = model
        self.query_one(Composer).set_model_name(model)

        arrival = f"Now answering with {model}"
        if provider.is_named:
            arrival += f" via {provider.label}"
        if save_selection(StoredSelection(model=model, provider=provider.name)):
            self.notify(arrival)
        else:
            self.notify(
                f"{arrival}, but the choice could not be saved.",
                severity="warning",
            )

        if not self.needs_credential():
            return
        self.run_worker(self._collect_credential(), exclusive=False)

    def needs_credential(self) -> bool:
        return not _usable(self.agent.llm_client.settings.credential)

    async def _accept_invitation(self, invitation: ProviderInvite) -> None:
        provider = self._provider_named(invitation.provider)
        if not await self._collect_credential(provider):
            return
        await self._choose_model(refresh=True)

    def _adopt(self, provider, credential) -> None:
        settings = self.agent.llm_client.settings
        if provider.name == settings.provider.name:
            settings.use(provider, credential)

    async def _collect_credential(self, provider=None) -> bool:
        target = provider or self.agent.llm_client.settings.provider
        if target.oauth is None:
            return await self._ask_for_api_key(target)

        chosen = await self._ask_how_to_connect(target)
        if chosen is None:
            self.notify(
                f"{target.name} is still not connected, so requests will fail.",
                severity="warning",
                timeout=10,
            )
            return False
        if chosen == OAUTH_CHOICE:
            return await self._authorise_provider(target)
        if chosen == DEVICE_CHOICE:
            return await self._authorise_by_code(target)
        return await self._ask_for_api_key(target)

    async def _ask_how_to_connect(self, provider) -> str | None:
        client = provider.oauth
        return await self.push_screen_wait(
            CredentialChoiceScreen(
                provider.label,
                self._model_for(provider),
                device=client is not None and client.supports_device_code,
            )
        )

    def _model_for(self, provider) -> str:
        settings = self.agent.llm_client.settings
        return self._model_name if provider.name == settings.provider.name else ""

    async def _ask_for_api_key(self, provider=None, *, reason: str = "") -> bool:
        from truecoder.providers.keys import store_key
        from truecoder.providers.models import ApiKey, CredentialError

        target = provider or self.agent.llm_client.settings.provider
        typed = await self.push_screen_wait(
            ApiKeyScreen(
                target.label,
                self._model_for(target),
                reason,
                browser_sign_in=target.oauth is not None,
            )
        )
        if not typed:
            self.notify(
                f"{target.name} still has no key, so requests will fail.",
                severity="warning",
                timeout=10,
            )
            return False

        try:
            key = ApiKey(typed)
        except CredentialError as error:
            self.notify(f"That key was refused: {error}", severity="error")
            return False

        self._adopt(target, key)
        if store_key(target.name, key):
            self.notify(f"Saved the key for {target.name}.")
        else:
            self.notify(
                "The key works for this session, but it could not be saved.",
                severity="warning",
            )
        return True

    async def _authorise_provider(self, provider=None) -> bool:
        from truecoder.providers.login import begin_login, open_in_browser
        from truecoder.providers.oauth import OAuthError
        from truecoder.providers.tokens import store_token

        target = provider or self.agent.llm_client.settings.provider
        client = target.oauth
        if client is None:
            self.notify(
                f"No OAuth client is configured for {target.name!r}. "
                "Add one in providers.json, or set an API key instead.",
                severity="warning",
                timeout=12,
            )
            return False

        try:
            pending = await begin_login(client, provider=target.name)
        except OAuthError as error:
            self.notify(f"Authorisation failed: {error}", severity="error", timeout=12)
            return False

        opened = open_in_browser(pending.url)
        await self._note_sign_in(target.name, pending.url, opened=opened)
        screen = AuthorisationScreen(target.label, pending.url, browser_opened=opened)
        waiting = asyncio.ensure_future(pending.wait())
        cancelled = self.push_screen_wait(screen)

        try:
            token = await self._await_authorisation(waiting, cancelled, screen)
        except OAuthError as error:
            self.notify(f"Authorisation failed: {error}", severity="error", timeout=12)
            return False
        finally:
            await pending.close()

        if token is None:
            self.notify("Authorisation cancelled.", severity="warning")
            return False

        self._adopt(target, token)
        if store_token(token):
            self.notify(f"Signed in to {target.name}.")
        else:
            self.notify(
                "Signed in, but the token could not be saved.",
                severity="warning",
            )
        return True

    async def _authorise_by_code(self, provider) -> bool:
        from truecoder.providers.device import poll_device_grant, request_device_grant
        from truecoder.providers.login import open_in_browser
        from truecoder.providers.oauth import OAuthError
        from truecoder.providers.tokens import store_token

        client = provider.oauth
        if client is None or not client.supports_device_code:
            self.notify(
                f"{provider.name} does not offer a device code flow.",
                severity="warning",
            )
            return False

        try:
            grant = await request_device_grant(client)
        except OAuthError as error:
            self.notify(f"Authorisation failed: {error}", severity="error", timeout=12)
            return False

        opened = open_in_browser(grant.best_url)
        await self._note_device_code(provider.label, grant)
        screen = DeviceCodeScreen(
            provider.label,
            grant.user_code,
            grant.best_url,
            browser_opened=opened,
        )
        waiting = asyncio.ensure_future(
            poll_device_grant(client, grant, provider=provider.name)
        )
        cancelled = self.push_screen_wait(screen)

        try:
            token = await self._await_authorisation(waiting, cancelled, screen)
        except OAuthError as error:
            self.notify(f"Authorisation failed: {error}", severity="error", timeout=12)
            return False

        if token is None:
            self.notify("Authorisation cancelled.", severity="warning")
            return False

        self._adopt(provider, token)
        if store_token(token):
            self.notify(f"Signed in to {provider.name}.")
        else:
            self.notify(
                "Signed in, but the token could not be saved.",
                severity="warning",
            )
        return True

    async def _note_device_code(self, provider: str, grant) -> None:
        note = SystemNote(
            SIGN_IN_HEADING,
            f"Enter the code {grant.user_code} to authorise {provider}.",
            grant.best_url,
        )
        await self._show_note(note)

    async def _note_sign_in(self, provider: str, url: str, *, opened: bool) -> None:
        from truecoder.tui.credentials import (
            NOTE_WITH_BROWSER,
            NOTE_WITHOUT_BROWSER,
        )

        await self._show_note(
            SystemNote(
                SIGN_IN_HEADING,
                f"Authorise TrueCoder with {provider}. "
                f"{NOTE_WITH_BROWSER if opened else NOTE_WITHOUT_BROWSER}",
                url,
            )
        )

    async def _show_note(self, note: SystemNote) -> None:
        self.query_one("#empty-state", EmptyState).styles.display = "none"
        self.screen.remove_class("empty-chat")
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(note)
        self._scroll_to_latest()

    async def _await_authorisation(self, waiting, cancelled, screen):
        from truecoder.providers.oauth import OAuthError

        dismissal = asyncio.ensure_future(cancelled)
        done, _ = await asyncio.wait(
            {waiting, dismissal},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if waiting in done:
            if screen.is_running:
                screen.dismiss(True)
            with suppress(asyncio.CancelledError):
                await dismissal
            return waiting.result()

        waiting.cancel()
        with suppress(asyncio.CancelledError, OAuthError):
            await waiting
        return None

    def _forget_authorisation(self) -> None:
        from truecoder.providers.keys import forget_key
        from truecoder.providers.tokens import forget_token

        settings = self.agent.llm_client.settings
        name = settings.provider.name
        forgotten = [
            label
            for label, gone in (
                ("authorisation", forget_token(name)),
                ("API key", forget_key(name)),
            )
            if gone
        ]

        if not forgotten:
            self.notify(f"Nothing stored for {name!r}.", severity="warning")
            return

        settings.use(settings.provider, None)
        self.notify(f"Forgot the stored {' and '.join(forgotten)} for {name!r}.")

    @on(ExecutionStageMessage)
    async def advance_execution_card(self, message: ExecutionStageMessage) -> None:
        event = message.event
        details = dict(event.details)
        card = await self._execution_card(
            event.execution_id,
            command=details.get("command"),
        )
        if card is None:
            return
        card.apply_stage(event.stage, event.message)
        if is_terminal_stage(event.stage):
            self._execution_cards.pop(event.execution_id, None)
        self._scroll_to_latest()

    @on(ExecutionOutputMessage)
    def append_execution_output(self, message: ExecutionOutputMessage) -> None:
        card = self._execution_cards.get(message.execution_id)
        if card is None:
            return
        card.append_output(message.stream, message.text)

    @on(ExecutionCard.CancelRequested)
    async def cancel_one_execution(
        self,
        message: ExecutionCard.CancelRequested,
    ) -> None:
        message.stop()
        await self._cancel_execution(
            message.execution_id,
            reason="cancelled from the interface",
        )

    async def _cancel_execution(self, execution_id: str, *, reason: str) -> None:
        service = self._execution_service()
        if service is None:
            return
        card = self._execution_cards.get(execution_id)
        if card is not None:
            card.mark_cancelling()
        try:
            await service.cancel(execution_id, reason=reason)
        except Exception as error:  # noqa: BLE001 - cancellation is a request
            self.notify(f"Cancellation failed: {error}", severity="warning")

    async def _execution_card(
        self,
        execution_id: str,
        *,
        command: str | None = None,
    ) -> ExecutionCard | None:
        card = self._execution_cards.get(execution_id)
        if card is not None:
            if command:
                card.set_command(command)
            return card
        if not self.is_mounted:
            return None

        card = ExecutionCard(execution_id, command or "shell command")
        self._execution_cards[execution_id] = card
        transcript = self.query_one("#transcript", VerticalScroll)
        before = (
            self._active_assistant
            if self._active_assistant is not None and self._active_assistant.is_mounted
            else None
        )
        await transcript.mount(card, before=before)
        return card

    @on(Button.Pressed, ".approval-once")
    def approve_pending_tool_once(self) -> None:
        self._resolve_pending_approval(ApprovalScope.ONCE)

    @on(Button.Pressed, ".approval-session")
    def approve_pending_tool_for_session(self) -> None:
        self._resolve_pending_approval(ApprovalScope.SESSION)

    @on(Button.Pressed, ".approval-workspace")
    def approve_pending_tool_for_workspace(self) -> None:
        self._resolve_pending_approval(ApprovalScope.WORKSPACE)

    @on(Button.Pressed, ".approval-reject")
    def reject_pending_tool(self) -> None:
        self._resolve_pending_approval(None)

    async def _submit_prompt(self, raw_prompt: str) -> None:
        prompt = raw_prompt.strip()
        if not prompt:
            return
        if self._busy:
            return

        self.screen.remove_class("empty-chat")
        self.query_one(StatusBar).set_conversation_active(True)
        prompt_input = self.query_one(PromptInput)
        prompt_input.text = ""

        self.query_one("#empty-state", EmptyState).styles.display = "none"
        transcript = self.query_one("#transcript", VerticalScroll)

        user_message = ChatMessage(
            "user",
            prompt,
            model_name=self._model_name,
        )
        assistant_message = ChatMessage(
            "assistant",
            model_name=self._model_name,
        )
        await transcript.mount(user_message, assistant_message)
        self._active_assistant = assistant_message
        self.call_after_refresh(
            transcript.scroll_end,
            animate=False,
            immediate=True,
        )

        self._set_busy(True)
        self._active_worker = self._stream_completion(
            prompt,
            assistant_message,
        )

    @work(group="completion", exclusive=True, exit_on_error=False)
    async def _stream_completion(
        self,
        prompt: str,
        assistant_message: ChatMessage,
    ) -> None:
        response_text = ""
        usage: TokenUsage | None = None
        finish_reason: str | None = None
        completed = False
        outcome = "ready"
        repair = ""

        try:
            async for event in self.agent.run(prompt):
                if event.type == AgentEventType.TEXT_DELTA:
                    content = str(event.data.get("content", ""))
                    response_text += content
                    await assistant_message.append_delta(content)
                    self._scroll_to_latest()
                elif event.type == AgentEventType.TOOL_CALL:
                    call_id = str(event.data.get("call_id", ""))
                    tool_name = str(event.data.get("name", "tool"))
                    arguments = str(event.data.get("arguments", "{}"))
                    if tool_name == UpdatePlanTool.name:
                        self._plan_calls[call_id] = arguments
                    else:
                        assistant_message = await self._show_tool_call(
                            call_id=call_id,
                            tool_name=tool_name,
                            arguments=arguments,
                            assistant_message=assistant_message,
                        )
                elif event.type == AgentEventType.APPROVAL_REQUESTED:
                    # The card is moved into approval by _request_tool_approval,
                    # which the agent always calls straight after this event.
                    # Showing the controls here would arm them before anything
                    # is listening, and a click landing in that window is lost.
                    self._scroll_to_latest()
                elif event.type == AgentEventType.TOOL_RESULT:
                    call_id = str(event.data.get("call_id", ""))
                    status = str(event.data.get("status", "error"))
                    content = str(event.data.get("content", ""))
                    if call_id in self._plan_calls:
                        assistant_message = await self._resolve_plan_call(
                            call_id=call_id,
                            status=status,
                            content=content,
                            assistant_message=assistant_message,
                        )
                    else:
                        self._finish_tool_call(
                            call_id=call_id,
                            status=status,
                            content=content,
                        )
                elif event.type == AgentEventType.TOOL_REJECTED:
                    call_id = str(event.data.get("call_id", ""))
                    content = str(event.data.get("content", ""))
                    if call_id in self._plan_calls:
                        assistant_message = await self._resolve_plan_call(
                            call_id=call_id,
                            status="rejected",
                            content=content,
                            assistant_message=assistant_message,
                        )
                    else:
                        self._reject_tool_call(call_id=call_id, content=content)
                elif event.type == AgentEventType.PROGRESS_STALLED:
                    self.notify(
                        "The agent was repeating itself and was asked to answer "
                        "with what it had.",
                        severity="warning",
                        timeout=8,
                    )
                elif event.type == AgentEventType.AGENT_END:
                    usage_data = event.data.get("usage")
                    usage = (
                        TokenUsage(**usage_data)
                        if isinstance(usage_data, dict)
                        else None
                    )
                    finish_reason_value = event.data.get("finish_reason")
                    finish_reason = (
                        str(finish_reason_value)
                        if finish_reason_value is not None
                        else None
                    )
                    completed = True
                    self._announce_hooks()
                    await self._announce_changes()
                    if self.session_manager is not None:
                        try:
                            self.session_manager.save_completed_turns()
                        except SessionError as error:
                            self.notify(
                                f"Session could not be saved: {error}",
                                severity="error",
                            )
                elif event.type == AgentEventType.AGENT_ERROR:
                    reported = str(
                        event.data.get("error")
                        or "The request failed without an error message."
                    )
                    details = event.data.get("details")
                    kind = str(
                        details.get("kind", "") if isinstance(details, dict) else ""
                    )
                    await assistant_message.show_error(
                        self._explained_failure(reported, kind)
                    )
                    repair = kind
                    outcome = "error"
                    break

            if outcome != "error":
                if not completed:
                    await assistant_message.show_error(
                        "The response stream ended before completion."
                    )
                    outcome = "error"
                elif not response_text:
                    await assistant_message.show_error(
                        "The model completed without returning any text."
                    )
                    outcome = "error"
                else:
                    assistant_message.finish(usage, finish_reason)
                    self.query_one(StatusBar).set_usage(usage)
        except asyncio.CancelledError:
            await assistant_message.show_cancelled()
            outcome = "stopped"
        except Exception as error:  # noqa: BLE001 - render unexpected worker failures
            await assistant_message.show_error(str(error))
            outcome = "error"
        finally:
            if self._active_assistant is assistant_message:
                self._active_assistant = None
            self._set_busy(False)
            self._scroll_to_latest()
            self.query_one(PromptInput).focus()
            self._repair_credential(repair)

    def _provider_uses_oauth(self) -> bool:
        from truecoder.providers.models import CredentialError

        try:
            return self.agent.llm_client.settings.provider.oauth is not None
        except (RuntimeError, CredentialError):
            return False

    def _explained_failure(self, reported: str, kind: str) -> str:
        from truecoder.client.failures import remedy

        advice = remedy(kind, oauth=self._provider_uses_oauth())
        return f"{reported}\n\n{advice}" if advice else reported

    def _repair_credential(self, kind: str) -> None:
        from truecoder.client.failures import CREDENTIAL

        if kind != CREDENTIAL:
            return
        self.run_worker(self._replace_rejected_credential(), exclusive=False)

    async def _replace_rejected_credential(self) -> None:
        from truecoder.client.failures import named
        from truecoder.tui.credentials import KEY_CHOICE

        provider = self.agent.llm_client.settings.provider
        rejected = f"{named(provider.name)} rejected the credential in use."
        if provider.oauth is None:
            await self._ask_for_api_key(
                provider,
                reason=f"{rejected} Enter another key.",
            )
            return

        chosen = await self.push_screen_wait(
            CredentialChoiceScreen(provider.label, self._model_name)
        )
        if chosen is None:
            return
        if chosen == KEY_CHOICE:
            await self._ask_for_api_key(
                provider,
                reason=f"{rejected} Enter another key.",
            )
            return
        await self._authorise_provider(provider)

    async def _request_tool_approval(
        self,
        request: ApprovalRequest,
    ) -> ApprovalResponse:
        future: asyncio.Future[ApprovalResponse] = (
            asyncio.get_running_loop().create_future()
        )
        allowed_scopes = tuple(scope.value for scope in request.allowed_scopes)
        card = self._tool_cards.get(request.call_id)
        restored = card is not None

        if card is None:
            card = ToolCallCard(
                request.call_id,
                request.tool_name,
                request.arguments,
                state="awaiting-approval",
                allowed_approval_scopes=allowed_scopes,
                mutation=request.mutation,
            )
            card.approval_details = self._approval_detail_rows(request)
            self._tool_cards[request.call_id] = card

        # Registered before the controls can be reached, so a decision can
        # never arrive while _resolve_pending_approval would discard it.
        self._pending_approval = _PendingApproval(
            request,
            future,
            card,
        )

        if restored:
            card.set_awaiting_approval(
                request.arguments,
                allowed_scopes=allowed_scopes,
                approval_details=self._approval_detail_rows(request),
                mutation=request.mutation,
            )
        else:
            transcript = self.query_one("#transcript", VerticalScroll)
            before = (
                self._active_assistant
                if self._active_assistant is not None
                and self._active_assistant.is_mounted
                else None
            )
            await transcript.mount(card, before=before)

        self._attach_execution_approval(request)

        card.query_one(".approval-once", Button).focus(scroll_visible=False)
        self.call_after_refresh(
            card.scroll_visible,
            animate=False,
            top=True,
            immediate=True,
        )

        try:
            return await future
        finally:
            self._clear_pending_approval()

    def _resolve_pending_approval(
        self,
        scope: ApprovalScope | None,
    ) -> None:
        pending = self._pending_approval
        if pending is None or pending.future.done():
            return

        if scope is not None and scope not in pending.request.allowed_scopes:
            return
        response = (
            ApprovalResponse.reject()
            if scope is None
            else ApprovalResponse.approve(scope)
        )
        pending.card.set_state("running" if scope is not None else "rejected")
        pending.future.set_result(response)
        self._clear_pending_approval()

    def _clear_pending_approval(self) -> None:
        self._pending_approval = None

    @staticmethod
    def _approval_detail_rows(
        request: ApprovalRequest,
    ) -> tuple[tuple[str, str], ...]:
        execution = request.execution
        if execution is None:
            return ()
        return compact_approval_rows(
            execution,
            tuple(scope.value for scope in request.allowed_scopes),
        )

    def _attach_execution_approval(self, request: ApprovalRequest) -> None:
        execution = request.execution
        if execution is None:
            return
        card = self._execution_cards.get(execution.execution_id)
        if card is None:
            return
        card.set_command(execution.command_display)
        card.call_id = request.call_id
        card.set_approval(
            compact_approval_rows(
                execution,
                tuple(scope.value for scope in request.allowed_scopes),
            ),
            full_approval_rows(execution),
        )

    async def _mount_after_assistant(
        self,
        widget: ToolCallCard | PlanCard,
        assistant_message: ChatMessage,
    ) -> ChatMessage:
        if assistant_message.content_text:
            assistant_message.finish_segment()
        else:
            await assistant_message.remove()

        transcript = self.query_one("#transcript", VerticalScroll)
        next_assistant = ChatMessage(
            "assistant",
            model_name=self._model_name,
        )
        await transcript.mount(widget, next_assistant)
        self._active_assistant = next_assistant
        self._scroll_to_latest()
        return next_assistant

    async def _show_tool_call(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: str,
        assistant_message: ChatMessage,
    ) -> ChatMessage:
        card = ToolCallCard(
            call_id,
            tool_name,
            arguments,
            state="running",
        )
        self._tool_cards[call_id] = card
        return await self._mount_after_assistant(card, assistant_message)

    async def _resolve_plan_call(
        self,
        *,
        call_id: str,
        status: str,
        content: str,
        assistant_message: ChatMessage,
    ) -> ChatMessage:
        arguments = self._plan_calls.pop(call_id, "{}")
        plan = self._current_plan()

        if status != "success" or plan is None:
            next_assistant = await self._show_tool_call(
                call_id=call_id,
                tool_name=UpdatePlanTool.name,
                arguments=arguments,
                assistant_message=assistant_message,
            )
            self._finish_tool_call(
                call_id=call_id,
                status=status,
                content=content,
            )
            return next_assistant

        self.query_one(StatusBar).set_plan(plan)

        if self._plan_card is None:
            self._plan_card = PlanCard(plan)
            return await self._mount_after_assistant(
                self._plan_card,
                assistant_message,
            )

        self._plan_card.update_plan(plan)
        self._scroll_to_latest()
        return assistant_message

    def _current_plan(self) -> Plan | None:
        store = self.agent.plan_store
        return None if store is None else store.current

    def _clear_plan(self) -> None:
        self._plan_card = None
        self._plan_calls.clear()
        if self.agent.plan_store is not None:
            self.agent.plan_store.clear()

    def _finish_tool_call(
        self,
        *,
        call_id: str,
        status: str,
        content: str,
    ) -> None:
        card = self._tool_cards.get(call_id)
        if card is not None:
            card.finish(status, content)
            self._scroll_to_latest()

    def _reject_tool_call(self, *, call_id: str, content: str) -> None:
        card = self._tool_cards.get(call_id)
        if card is not None:
            card.reject(content)
            self._scroll_to_latest()

    def _scroll_to_latest(self) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        self.call_after_refresh(
            transcript.scroll_end,
            animate=False,
            immediate=True,
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        try:
            self.query_one(Composer).set_busy(busy)
        except NoMatches:
            return

    async def action_new_chat(self) -> None:
        active_worker = self._active_worker
        if active_worker is not None and active_worker.is_running:
            active_worker.cancel()
            try:
                await active_worker.wait()
            except WorkerCancelled:
                pass
        if self.session_manager is None:
            self.agent.reset()
        else:
            self.session_manager.create_session()
        self._clear_pending_approval()
        await self.query(".chat-message").remove()
        await self.query(".tool-call-card").remove()
        await self.query(".execution-card").remove()
        await self.query(".plan-card").remove()
        await self.query(".system-note").remove()
        self._tool_cards.clear()
        self._execution_cards.clear()
        self._clear_plan()
        self._active_assistant = None
        self.query_one("#empty-state", EmptyState).styles.display = "block"
        self._set_busy(False)
        self.query_one(StatusBar).reset()
        self.screen.add_class("empty-chat")
        self.query_one(PromptInput).focus()

    def action_manage_sessions(self) -> None:
        if self.session_manager is None:
            self.notify("Session storage is not configured.", severity="warning")
            return
        if self._busy:
            self.notify(
                "Stop the current response before managing sessions.",
                severity="warning",
            )
            return
        self.push_screen(
            SessionManagerScreen(
                self.session_manager.list_sessions(),
                self.session_manager.active_session.session_id,
            ),
            self._handle_session_action,
        )

    async def action_manage_audit(self) -> None:
        runtime = self.agent.execution_runtime
        audit = runtime.audit if runtime is not None else None
        if audit is None:
            self.notify("Execution audit is unavailable.", severity="warning")
            return
        if self._busy:
            self.notify(
                "Stop the current response before viewing the audit.",
                severity="warning",
            )
            return
        project_root = (
            self.session_manager.project_root
            if self.session_manager is not None
            else Path.cwd().resolve()
        )
        try:
            snapshots = await audit.list_runs(
                workspace_id=workspace_id_for(project_root),
            )
            events = await asyncio.gather(
                *(audit.get_events(item.record.run_id) for item in snapshots)
            )
        except Exception as error:  # noqa: BLE001
            self.notify(f"Audit could not be read: {error}", severity="error")
            return
        self.push_screen(
            AuditViewerScreen(
                tuple(
                    audit_row_from(snapshot, run_events)
                    for snapshot, run_events in zip(
                        snapshots,
                        events,
                        strict=True,
                    )
                )
            )
        )

    async def action_review_changes(self) -> None:
        service = self.agent.checkpoints
        if service is None:
            self.notify("Checkpoints are not configured.", severity="warning")
            return
        if isinstance(self.screen, WorkspaceChangesScreen):
            return
        if self._busy:
            self.notify(
                "Stop the current response before reviewing changes.",
                severity="warning",
            )
            return

        try:
            reason = await service.unavailable_reason()
            changes = None if reason is not None else await self.agent.turn_changes()
        except Exception as error:  # noqa: BLE001 - report a failed comparison
            self.notify(f"Changes could not be read: {error}", severity="error")
            return

        self.push_screen(WorkspaceChangesScreen(changes, unavailable_reason=reason))

    def _announce_hooks(self) -> None:
        failures = [outcome for outcome in self.agent.hook_outcomes if not outcome.ok]
        if not failures:
            return

        described = ", ".join(outcome.summary for outcome in failures)
        self.notify(f"Hook: {described}", severity="warning", timeout=10)

    async def _announce_changes(self) -> None:
        service = self.agent.checkpoints
        if service is None:
            return

        try:
            changes = await self.agent.turn_changes()
        except Exception:  # noqa: BLE001 - a summary never disturbs a finished turn
            return

        if changes is None or changes.is_empty:
            return

        self.notify(
            f"{changes.summary}. Press ctrl+d to review.",
            timeout=8,
        )

    def action_manage_memory(self) -> None:
        store = self.agent.memory_store
        if store is None:
            self.notify("Memory is not configured.", severity="warning")
            return

        try:
            entries = store.entries()
        except Exception as error:  # noqa: BLE001 - report an unreadable store
            self.notify(f"Memory could not be read: {error}", severity="error")
            return

        self.push_screen(
            MemoryBrowserScreen(entries),
            self._handle_memory_action,
        )

    def _handle_memory_action(self, action: MemoryAction | None) -> None:
        store = self.agent.memory_store
        if action is None or store is None:
            return

        try:
            if action.kind == "clear":
                removed = store.clear()
                self.notify(f"Forgot {removed} note(s).")
                return
            if action.entry_id is not None and store.forget(action.entry_id):
                self.notify("Forgot that note.")
        except Exception as error:  # noqa: BLE001 - report a failed write
            self.notify(f"Memory could not be changed: {error}", severity="error")

    async def action_manage_checkpoints(self) -> None:
        service = self.agent.checkpoints
        if service is None:
            self.notify("Checkpoints are not configured.", severity="warning")
            return
        if self._busy:
            self.notify(
                "Stop the current response before restoring a checkpoint.",
                severity="warning",
            )
            return

        reason = await service.unavailable_reason()
        checkpoints = () if reason is not None else await service.list()
        self.push_screen(
            CheckpointBrowserScreen(checkpoints, unavailable_reason=reason),
            self._handle_checkpoint_choice,
        )

    async def _handle_checkpoint_choice(self, checkpoint_id: str | None) -> None:
        service = self.agent.checkpoints
        if checkpoint_id is None or service is None:
            return

        checkpoint = await service.find(checkpoint_id)
        if checkpoint is None:
            self.notify("That checkpoint no longer exists.", severity="warning")
            return

        removals = await service.preview(checkpoint)
        self.push_screen(
            RestoreCheckpointScreen(checkpoint, removals),
            lambda confirmed: self._finish_restore(checkpoint_id, confirmed),
        )

    def _finish_restore(self, checkpoint_id: str, confirmed: bool | None) -> None:
        if confirmed:
            self.run_worker(self._restore_checkpoint(checkpoint_id))

    async def _restore_checkpoint(self, checkpoint_id: str) -> None:
        service = self.agent.checkpoints
        if service is None:
            return

        try:
            outcome = await service.restore(checkpoint_id)
        except Exception as error:  # noqa: BLE001 - report a failed restore
            self.notify(f"Restore failed: {error}", severity="error", timeout=10)
            return

        self.agent.turn_checkpoint = None
        self.notify(outcome.summary, timeout=10)

    def action_execution_health(self) -> None:
        runtime = self.agent.execution_runtime
        if runtime is None:
            self.notify("Execution subsystem is not configured.", severity="warning")
            return
        self.push_screen(ExecutionHealthScreen(runtime.health))

    async def _handle_session_action(
        self,
        action: SessionAction | None,
    ) -> None:
        if action is None or self.session_manager is None:
            return
        try:
            if action.kind == "new":
                self.session_manager.create_session()
                await self._render_active_session()
            elif action.kind == "switch" and action.session_id is not None:
                record = self.session_manager.switch_session(action.session_id)
                await self._render_session(record)
            elif action.kind == "rename" and action.session_id is not None:
                summary = self._session_summary(action.session_id)
                self.push_screen(
                    RenameSessionScreen(summary.title),
                    lambda title: self._finish_session_rename(
                        action.session_id,
                        title,
                    ),
                )
            elif action.kind == "delete" and action.session_id is not None:
                summary = self._session_summary(action.session_id)
                self.push_screen(
                    DeleteSessionScreen(summary.title),
                    lambda confirmed: self._finish_session_delete(
                        action.session_id,
                        confirmed,
                    ),
                )
        except SessionError as error:
            self.notify(f"Session operation failed: {error}", severity="error")

    def _session_summary(self, session_id: str):
        if self.session_manager is None:
            raise RuntimeError("Session storage is not configured.")
        return next(
            summary
            for summary in self.session_manager.list_sessions()
            if summary.session_id == session_id
        )

    def _finish_session_rename(
        self,
        session_id: str,
        title: str | None,
    ) -> None:
        if title is None or self.session_manager is None:
            return
        try:
            self.session_manager.rename_session(session_id, title)
        except (SessionError, ValueError) as error:
            self.notify(f"Session could not be renamed: {error}", severity="error")

    async def _finish_session_delete(
        self,
        session_id: str,
        confirmed: bool,
    ) -> None:
        if not confirmed or self.session_manager is None:
            return
        deleting_active = session_id == self.session_manager.active_session.session_id
        try:
            self.session_manager.delete_session(session_id)
            if deleting_active:
                await self._render_active_session()
        except SessionError as error:
            self.notify(f"Session could not be deleted: {error}", severity="error")

    async def _render_active_session(self) -> None:
        if self.session_manager is None:
            return
        record = SessionRecord(
            summary=self.session_manager.active_session,
            completed_turns=tuple(
                tuple(turn) for turn in self.agent.state.completed_turns
            ),
        )
        await self._render_session(record)

    async def _render_session(self, record: SessionRecord) -> None:
        self.agent.turn_checkpoint = None
        await self.query(".chat-message").remove()
        await self.query(".tool-call-card").remove()
        await self.query(".execution-card").remove()
        await self.query(".plan-card").remove()
        await self.query(".system-note").remove()
        self._tool_cards.clear()
        self._execution_cards.clear()
        self._clear_plan()
        self._active_assistant = None
        transcript = self.query_one("#transcript", VerticalScroll)
        cards: dict[str, ToolCallCard] = {}

        for turn in record.completed_turns:
            for message in turn:
                role = message["role"]
                if role == "user":
                    await transcript.mount(
                        ChatMessage(
                            "user",
                            message["content"],
                            model_name=self._model_name,
                        )
                    )
                elif role == "assistant" and "tool_calls" in message:
                    if message["content"]:
                        segment = ChatMessage(
                            "assistant",
                            message["content"],
                            model_name=self._model_name,
                        )
                        await transcript.mount(segment)
                        segment.finish_segment()
                    for raw_call in message["tool_calls"]:
                        function = raw_call["function"]
                        if function["name"] == UpdatePlanTool.name:
                            continue
                        card = ToolCallCard(
                            raw_call["id"],
                            function["name"],
                            function["arguments"],
                            state="running",
                        )
                        cards[raw_call["id"]] = card
                        self._tool_cards[raw_call["id"]] = card
                        await transcript.mount(card)
                elif role == "tool":
                    card = cards.get(message["tool_call_id"])
                    if card is not None:
                        try:
                            payload = json.loads(message["content"])
                        except json.JSONDecodeError:
                            payload = {}
                        status = (
                            "approval_rejected"
                            if payload.get("error_code") == "approval_rejected"
                            else str(payload.get("status", "error"))
                        )
                        card.restore_result(status, message["content"])
                elif role == "assistant":
                    restored = ChatMessage(
                        "assistant",
                        message["content"],
                        model_name=self._model_name,
                    )
                    await transcript.mount(restored)
                    restored.restore()

        has_history = bool(record.completed_turns)
        self.query_one("#empty-state", EmptyState).styles.display = (
            "none" if has_history else "block"
        )
        self.screen.set_class(not has_history, "empty-chat")
        status_bar = self.query_one(StatusBar)
        status_bar.reset()
        status_bar.set_conversation_active(has_history)
        self._set_busy(False)
        self._scroll_to_latest()
        self.query_one(PromptInput).focus()

    async def action_cancel_response(self) -> None:
        if self._pending_approval is not None:
            self._resolve_pending_approval(None)
            return
        if await self._cancel_active_executions(reason="stopped by the user"):
            return
        if self._active_worker is not None and self._active_worker.is_running:
            self._active_worker.cancel()
        else:
            self.query_one(PromptInput).focus()
