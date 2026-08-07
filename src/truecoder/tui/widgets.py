from __future__ import annotations

import json
from time import monotonic
from typing import Any, ClassVar, Final

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Button, Markdown, Static, TextArea

from truecoder.client.response import TokenUsage
from truecoder.mutation import DIFF_LINE_PREFIXES, DiffLineKind, FileDiff
from truecoder.planning import Plan, PlanStepStatus
from truecoder.tui.execution_view import (
    EXECUTION_CARD_STATES,
    BoundedPreview,
    stage_presentation,
)

ASCII_LOGO = (
    "╺┳╸┏━┓╻ ╻┏━╸┏━╸┏━┓╺┳┓┏━╸┏━┓",
    " ┃ ┣┳┛┃ ┃┣╸ ┃  ┃ ┃ ┃┃┣╸ ┣┳┛",
    " ╹ ╹┗╸┗━┛┗━╸┗━╸┗━┛╺┻┛┗━╸╹┗╸",
)
_LOGO_NAME_BREAK = 12


def _logo_text() -> Text:
    """Render the wordmark with the two-tone treatment used by the launcher."""
    logo = Text()
    for index, line in enumerate(ASCII_LOGO):
        logo.append(line[:_LOGO_NAME_BREAK], style="#777777")
        logo.append(line[_LOGO_NAME_BREAK:], style="bold #dddddd")
        if index < len(ASCII_LOGO) - 1:
            logo.append("\n")
    return logo


def _session_metadata(
    model_name: str,
    *,
    elapsed: float | None = None,
    state: str | None = None,
) -> Text:
    """Build the shared composer/turn metadata without markup interpolation."""
    metadata = Text()
    if state is not None:
        glyph = {
            "error": "■",
            "stopped": "■",
        }.get(state, "▣")
        color = "#ef6f78" if state in {"error", "stopped"} else "#4da3ff"
        metadata.append(f"{glyph}  ", style=color)
    metadata.append("Build", style="bold #4da3ff")
    metadata.append("  ·  ", style="#666666")
    metadata.append(model_name, style="bold #d6d6d6")
    metadata.append("  ·  ", style="#666666")
    metadata.append("xhigh", style="bold #f2a33a")
    if elapsed is not None:
        metadata.append("  ·  ", style="#666666")
        metadata.append(f"{elapsed:.1f}s", style="#777777")
    return metadata


def _launcher_shortcuts() -> Text:
    shortcuts = Text()
    shortcuts.append("tab", style="#c8c8c8")
    shortcuts.append(" agents    ", style="#707070")
    shortcuts.append("ctrl+p", style="#c8c8c8")
    shortcuts.append(" sessions    ", style="#707070")
    shortcuts.append("ctrl+q", style="#c8c8c8")
    shortcuts.append(" quit", style="#707070")
    return shortcuts


def _launcher_tip() -> Text:
    tip = Text()
    tip.append("●  ", style="#f2a33a")
    tip.append("Tip", style="bold #f2a33a")
    tip.append(
        " Use numeric xterm color codes 0-255 in custom theme JSON",
        style="#686868",
    )
    return tip

_TOOL_STATE_LABELS = {
    "queued": "Queued",
    "awaiting-approval": "Awaiting approval",
    "running": "Running",
    "completed": "Completed",
    "rejected": "Rejected",
    "failed": "Failed",
}

_TOOL_STATE_GLYPHS = {
    "queued": "◇",
    "awaiting-approval": "◇",
    "running": "◈",
    "completed": "✓",
    "rejected": "×",
    "failed": "!",
}

_RISKY_TOOL_TERMS = frozenset(
    {
        "command",
        "delete",
        "edit",
        "execute",
        "move",
        "patch",
        "remove",
        "rename",
        "shell",
        "write",
    }
)

_TARGET_KEYS = ("path", "file_path", "target", "command", "query", "url")
_APPROVAL_SCOPES = frozenset({"once", "session", "workspace"})


class PromptInput(TextArea):
    """Multiline prompt input with chat-style submission."""

    BINDINGS: ClassVar[list[Binding]] = [
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
            explicit_lines = self.text.count("\n") + 1
            visual_lines = max(explicit_lines, self.wrapped_document.height)
            self.styles.height = min(7, max(2, visual_lines + 1))


class EmptyState(Vertical):
    """Centered two-tone wordmark for an empty session."""

    def compose(self) -> ComposeResult:
        yield Static(_logo_text(), id="ascii-logo", markup=False)
        yield Static("truecoder", id="compact-logo", markup=False)


class Composer(Vertical):
    """OpenCode-inspired prompt composer used in both layout states."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        super().__init__(id="composer")

    def compose(self) -> ComposeResult:
        with Vertical(id="composer-shell"):
            yield PromptInput(
                id="prompt-input",
                placeholder='Ask anything...  "Fix broken tests"',
                soft_wrap=True,
                show_line_numbers=False,
                tab_behavior="focus",
                compact=True,
            )
            yield Static(
                _session_metadata(self.model_name),
                id="composer-metadata",
                markup=False,
            )
        yield Static(
            _launcher_shortcuts(),
            id="launcher-shortcuts",
            markup=False,
        )
        yield Static(_launcher_tip(), id="launcher-tip", markup=False)

    def set_busy(self, busy: bool) -> None:
        self.set_class(busy, "busy")


class ChatMessage(Vertical):
    """A user or assistant entry in the chronological timeline."""

    def __init__(
        self,
        role: str,
        content: str = "",
        *,
        model_name: str = "model",
    ) -> None:
        self.role = role
        self.content_text = content
        self.model_name = model_name
        self.started_at = monotonic()
        classes = f"chat-message {role}"
        super().__init__(classes=classes)

    def compose(self) -> ComposeResult:
        initial_content = self.content_text
        if self.role == "assistant" and not initial_content:
            initial_content = "_Thinking…_"

        yield Markdown(initial_content, classes="message-body")
        if self.role == "assistant":
            yield Static(
                _session_metadata(self.model_name, state="running"),
                classes="message-footer",
                markup=False,
            )

    async def append_delta(self, delta: str) -> None:
        if not self.content_text:
            self.set_class(True, "streaming")
        self.content_text += delta
        await self.query_one(".message-body", Markdown).update(self.content_text)

    def finish_segment(self) -> None:
        """Leave intermediate model text in place before a tool activity row."""
        self.remove_class("streaming")
        self.query_one(".message-footer", Static).styles.display = "none"

    def restore(self) -> None:
        """Mark persisted assistant text complete without inventing timing."""
        self.remove_class("streaming")
        self.add_class("completed")
        self.query_one(".message-footer", Static).update(
            _session_metadata(self.model_name, state="completed")
        )

    def finish(
        self,
        usage: TokenUsage | None,
        finish_reason: str | None = None,
    ) -> None:
        elapsed = monotonic() - self.started_at
        self.remove_class("streaming")
        self.add_class("completed")
        self.query_one(".message-footer", Static).update(
            _session_metadata(
                self.model_name,
                elapsed=elapsed,
                state="completed",
            )
        )

    async def show_error(self, error: str) -> None:
        self.remove_class("streaming")
        self.add_class("error")
        safe_error = error.replace("\\", "\\\\").replace("`", "\\`")
        self.content_text = f"**Request failed**\n\n{safe_error}"
        await self.query_one(".message-body", Markdown).update(self.content_text)
        self.query_one(".message-footer", Static).update(
            _session_metadata(self.model_name, state="error")
        )

    async def show_cancelled(self) -> None:
        self.remove_class("streaming")
        self.add_class("cancelled")
        if not self.content_text:
            self.content_text = "_Generation interrupted._"
            await self.query_one(".message-body", Markdown).update(self.content_text)
        self.query_one(".message-footer", Static).update(
            _session_metadata(self.model_name, state="stopped")
        )


_DIFF_HEADER_STYLE = "#707070"
_DIFF_TRUNCATION_STYLE = "#f2a33a"
_DIFF_LINE_STYLES: dict[DiffLineKind, str] = {
    "context": "#a2a2a2",
    "added": "#67c587",
    "removed": "#ef6f78",
}


def render_diff(diff: FileDiff) -> Text:
    text = Text()

    for hunk_index, hunk in enumerate(diff.hunks):
        if hunk_index:
            text.append("\n")
        text.append(f"{hunk.header}\n", style=_DIFF_HEADER_STYLE)
        for line in hunk.lines:
            number = (
                line.before_number if line.kind == "removed" else line.after_number
            )
            label = "    " if number is None else f"{number:>4}"
            text.append(
                f"{DIFF_LINE_PREFIXES[line.kind]} {label}  {line.text}\n",
                style=_DIFF_LINE_STYLES[line.kind],
            )

    if diff.newline_changed:
        text.append("\\ trailing newline changed\n", style=_DIFF_HEADER_STYLE)
    if diff.line_endings_changed:
        text.append("\\ line endings changed\n", style=_DIFF_TRUNCATION_STYLE)
    if diff.truncated:
        text.append(
            f"… diff truncated, {diff.summary}\n",
            style=_DIFF_TRUNCATION_STYLE,
        )
    return text


class ToolCallCard(Vertical):
    """Persistent tool activity with approval controls and expandable details."""

    def __init__(
        self,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | str,
        *,
        state: str = "queued",
        allowed_approval_scopes: tuple[str, ...] = ("once",),
        mutation: FileDiff | None = None,
    ) -> None:
        if state not in _TOOL_STATE_LABELS:
            raise ValueError(f"Unsupported tool-call state: {state}")
        if mutation is not None and not isinstance(mutation, FileDiff):
            raise TypeError("mutation must be a FileDiff or None")

        self.mutation = mutation
        self.call_id = call_id
        self.tool_name = tool_name
        self.arguments = self._parse_arguments(arguments)
        self.raw_arguments = (
            arguments
            if isinstance(arguments, str)
            else json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        )
        self.state = state
        self.result_content = ""
        self.approval_details: tuple[tuple[str, str], ...] = ()
        self.allowed_approval_scopes = self._validate_approval_scopes(
            allowed_approval_scopes
        )
        self.restored = False
        self.started_at = monotonic()
        self.running_at = self.started_at if state == "running" else None

        classes = f"tool-call-card state-{state}"
        if self._is_risky:
            classes += " risky"
        if not self._target and not self._parameter_summary:
            classes += " no-summary"
        if self.allowed_approval_scopes == ("once",):
            classes += " approval-once-only"
        super().__init__(classes=classes)

    @property
    def _is_risky(self) -> bool:
        words = set(self.tool_name.lower().replace("-", "_").split("_"))
        return bool(words & _RISKY_TOOL_TERMS)

    @property
    def _target(self) -> str:
        for key in _TARGET_KEYS:
            value = self.arguments.get(key)
            if value is not None:
                return str(value)
        return ""

    @property
    def _parameter_summary(self) -> str:
        if self.mutation is not None:
            return self.mutation.summary

        start_line = self.arguments.get("start_line")
        line_count = self.arguments.get("line_count")
        if isinstance(start_line, int) and isinstance(line_count, int):
            return f"lines {start_line}–{start_line + line_count - 1}"

        pairs: list[str] = []
        for key, value in self.arguments.items():
            if key in _TARGET_KEYS:
                continue
            display = str(value).replace("\n", " ")
            if len(display) > 24:
                display = f"{display[:21]}…"
            pairs.append(f"{key.replace('_', ' ')}={display}")
            if len(pairs) == 2:
                break
        return "  ·  ".join(pairs)

    @property
    def human_name(self) -> str:
        return self.tool_name.replace("_", " ").replace("-", " ").capitalize()

    def compose(self) -> ComposeResult:
        with Horizontal(classes="tool-heading"):
            yield Static(
                _TOOL_STATE_GLYPHS[self.state],
                classes="tool-state-glyph",
                markup=False,
            )
            yield Static(
                self._headline(),
                classes="tool-title",
                markup=False,
            )
            yield Static("", classes="tool-spacer")
            yield Static(
                _TOOL_STATE_LABELS[self.state],
                classes="tool-state-label",
                markup=False,
            )
            yield Button("Details", classes="tool-details-toggle")

        with Horizontal(classes="tool-summary"):
            yield Static(self._target, classes="tool-target", markup=False)
            yield Static(
                f" · {self._parameter_summary}"
                if self._target and self._parameter_summary
                else self._parameter_summary,
                classes="tool-parameters",
                markup=False,
            )

        with Horizontal(classes="tool-approval-actions"):
            yield Button(
                "Approve once",
                classes=self._approval_button_classes("once"),
            )
            yield Button(
                "Allow session",
                classes=self._approval_button_classes("session"),
            )
            yield Button(
                "Allow workspace",
                classes=self._approval_button_classes("workspace"),
            )
            yield Button("Reject", classes="approval-reject")

        with VerticalScroll(classes="tool-diff"):
            yield Static(self._diff_text(), classes="tool-diff-content", markup=False)

        with VerticalScroll(classes="tool-details"):
            yield Static(
                self._details_text(),
                classes="tool-details-content",
                markup=False,
            )

    def set_awaiting_approval(
        self,
        arguments: dict[str, Any],
        *,
        allowed_scopes: tuple[str, ...] = ("once",),
        approval_details: tuple[tuple[str, str], ...] = (),
        mutation: FileDiff | None = None,
    ) -> None:
        if mutation is not None and not isinstance(mutation, FileDiff):
            raise TypeError("mutation must be a FileDiff or None")

        self.arguments = arguments
        self.mutation = mutation
        self.allowed_approval_scopes = self._validate_approval_scopes(
            allowed_scopes
        )
        self.approval_details = approval_details
        self.running_at = None
        self.raw_arguments = json.dumps(
            arguments,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        self._sync_approval_buttons()
        self._refresh_summary()
        self._refresh_diff()
        self.set_state("awaiting-approval")

    def set_state(self, state: str) -> None:
        if state not in _TOOL_STATE_LABELS:
            raise ValueError(f"Unsupported tool-call state: {state}")
        previous_state = self.state
        self.remove_class(f"state-{self.state}")
        self.state = state
        if state == "running" and previous_state != "running":
            self.running_at = monotonic()
        self.add_class(f"state-{state}")
        if not self.is_mounted:
            return
        self.query_one(".tool-state-glyph", Static).update(_TOOL_STATE_GLYPHS[state])
        self.query_one(".tool-state-label", Static).update(_TOOL_STATE_LABELS[state])
        self.query_one(".tool-title", Static).update(self._headline())
        self.query_one(".tool-details-content", Static).update(self._details_text())

    def finish(self, status: str, content: str) -> None:
        self.result_content = content
        self.set_state("completed" if status == "success" else "failed")

    def reject(self, content: str) -> None:
        self.result_content = content
        self.set_state("rejected")

    def restore_result(self, status: str, content: str) -> None:
        """Restore a persisted result without fabricating elapsed time."""
        self.restored = True
        self.result_content = content
        if status == "approval_rejected":
            self.set_state("rejected")
        else:
            self.set_state("completed" if status == "success" else "failed")

    @on(Button.Pressed, ".tool-details-toggle")
    def toggle_details(self, event: Button.Pressed) -> None:
        event.stop()
        expanded = not self.has_class("expanded")
        self.set_class(expanded, "expanded")
        event.button.label = "Hide" if expanded else "Details"

    def _refresh_diff(self) -> None:
        self.set_class(self.mutation is not None, "has-diff")
        if not self.is_mounted:
            return
        try:
            self.query_one(".tool-diff-content", Static).update(self._diff_text())
        except NoMatches:
            return

    def _diff_text(self) -> Text:
        return Text() if self.mutation is None else render_diff(self.mutation)

    def _refresh_summary(self) -> None:
        if not self.is_mounted:
            return
        self.set_class(not self._target and not self._parameter_summary, "no-summary")
        self.query_one(".tool-target", Static).update(self._target)
        parameter_text = self._parameter_summary
        self.query_one(".tool-parameters", Static).update(
            f" · {parameter_text}"
            if self._target and parameter_text
            else parameter_text
        )
        self.query_one(".tool-details-content", Static).update(self._details_text())

    def _headline(self) -> str:
        if self.state not in {"completed", "rejected", "failed"}:
            return self.human_name

        if self.state == "rejected":
            prefix = f"Rejected {self.human_name.lower()}"
        elif self.state == "failed":
            prefix = f"Failed {self.human_name.lower()}"
        else:
            verbs = {
                "edit_file": "Edited",
                "glob": "Matched",
                "grep": "Searched",
                "read_file": "Read",
                "write_file": "Wrote",
                "list_dir": "Listed",
                "list_files": "Listed",
                "search_files": "Searched",
                "execute_command": "Ran",
                "run_shell": "Ran",
                "shell": "Ran",
            }
            prefix = verbs.get(self.tool_name, f"Completed {self.human_name.lower()}")

        headline = f"{prefix} {self._target}" if self._target else prefix
        parts = [headline]
        result_summary = self._result_summary()
        if result_summary:
            parts.append(result_summary)
        elapsed = self._elapsed_label()
        if elapsed:
            parts.append(elapsed)
        return " · ".join(parts)

    def _result_summary(self) -> str:
        try:
            payload = json.loads(self.result_content)
        except (json.JSONDecodeError, TypeError):
            return ""

        output = payload.get("output") if isinstance(payload, dict) else None
        if self.tool_name == "read_file" and isinstance(output, dict):
            start = output.get("start_line")
            end = output.get("end_line")
            if isinstance(start, int) and isinstance(end, int):
                count = max(0, end - start + 1)
                return f"{count:,} {'line' if count == 1 else 'lines'}"
        if self.tool_name == "write_file" and isinstance(output, dict):
            bytes_written = output.get("bytes_written")
            if isinstance(bytes_written, int) and not isinstance(bytes_written, bool):
                return (
                    f"{bytes_written:,} "
                    f"{'byte' if bytes_written == 1 else 'bytes'}"
                )
        collection_keys = {
            "glob": ("matches", "match", "matches"),
            "grep": ("matches", "match", "matches"),
            "list_dir": ("entries", "entry", "entries"),
        }
        collection_summary = collection_keys.get(self.tool_name)
        if collection_summary is not None and isinstance(output, dict):
            key, singular, plural = collection_summary
            values = output.get(key)
            if isinstance(values, list):
                count = len(values)
                return f"{count:,} {singular if count == 1 else plural}"
        if self.tool_name == "edit_file" and isinstance(output, dict):
            replacements = output.get("replacements")
            if isinstance(replacements, int) and not isinstance(replacements, bool):
                return (
                    f"{replacements:,} "
                    f"{'replacement' if replacements == 1 else 'replacements'}"
                )
        return ""

    def _elapsed_label(self) -> str:
        if self.restored:
            return ""
        timer_started_at = (
            self.running_at
            if self.state in {"completed", "failed"} and self.running_at is not None
            else self.started_at
        )
        milliseconds = max(1, round((monotonic() - timer_started_at) * 1000))
        if milliseconds < 1000:
            return f"{milliseconds} ms"
        return f"{milliseconds / 1000:.1f} s"

    def _details_text(self) -> str:
        argument_text = self.raw_arguments.strip() or "{}"
        try:
            parsed_arguments = json.loads(argument_text)
            argument_text = json.dumps(
                parsed_arguments,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        except json.JSONDecodeError:
            pass

        details = f"Arguments\n{argument_text}"
        if self.approval_details:
            approval_text = "\n".join(
                f"{name}: {value}" for name, value in self.approval_details
            )
            details += f"\n\nExecution approval\n{approval_text}"
        if self.result_content:
            result_text = self.result_content
            try:
                parsed_result = json.loads(result_text)
                result_text = json.dumps(
                    parsed_result,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            except json.JSONDecodeError:
                pass
            details += f"\n\nResult\n{result_text}"
        return details

    def _approval_button_classes(self, scope: str) -> str:
        classes = f"approval-{scope}"
        if scope not in self.allowed_approval_scopes:
            classes += " scope-disabled"
        return classes

    def _sync_approval_buttons(self) -> None:
        self.set_class(
            self.allowed_approval_scopes == ("once",),
            "approval-once-only",
        )
        if not self.is_mounted:
            return
        for scope in _APPROVAL_SCOPES:
            button = self.query_one(f".approval-{scope}", Button)
            button.set_class(
                scope not in self.allowed_approval_scopes,
                "scope-disabled",
            )

    @staticmethod
    def _validate_approval_scopes(scopes: object) -> tuple[str, ...]:
        if not isinstance(scopes, tuple):
            raise TypeError("allowed approval scopes must be a tuple")
        if "once" not in scopes:
            raise ValueError("allowed approval scopes must include 'once'")
        if len(scopes) != len(set(scopes)):
            raise ValueError("allowed approval scopes cannot contain duplicates")
        if any(scope not in _APPROVAL_SCOPES for scope in scopes):
            raise ValueError("unknown approval scope")
        return scopes

    @staticmethod
    def _parse_arguments(arguments: dict[str, Any] | str) -> dict[str, Any]:
        if isinstance(arguments, dict):
            return arguments
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {"arguments": arguments}
        return parsed if isinstance(parsed, dict) else {"arguments": parsed}


class ExecutionCard(Vertical):
    class CancelRequested(Message):
        def __init__(self, execution_id: str) -> None:
            self.execution_id = execution_id
            super().__init__()

    def __init__(
        self,
        execution_id: str,
        command: str,
        *,
        call_id: str | None = None,
    ) -> None:
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise ValueError("execution_id cannot be empty")

        self.execution_id = execution_id.strip()
        self.call_id = call_id
        self.command = command or "(no command)"
        self.state = "preparing"
        self.stage: str | None = None
        self.audit_id: str | None = None
        self.compact_rows: tuple[tuple[str, str], ...] = ()
        self.detail_rows: tuple[tuple[str, str], ...] = ()
        self.result_summary = ""
        self.cancel_requested = False
        self.started_at = monotonic()
        self._preview = BoundedPreview()

        super().__init__(classes="execution-card state-preparing")

    def compose(self) -> ComposeResult:
        with Horizontal(classes="execution-heading"):
            yield Static("◇", classes="execution-glyph", markup=False)
            yield Static(self._headline(), classes="execution-title", markup=False)
            yield Static("", classes="tool-spacer")
            yield Static("Preparing", classes="execution-state-label", markup=False)
            yield Button("Stop", classes="execution-cancel")
            yield Button("Details", classes="execution-details-toggle")

        yield Static(
            self._compact_text(),
            classes="execution-summary",
            markup=False,
        )

        with VerticalScroll(classes="execution-output"):
            yield Static("", classes="execution-output-content", markup=False)

        with VerticalScroll(classes="execution-details"):
            yield Static("", classes="execution-details-content", markup=False)

    def set_approval(
        self,
        compact_rows: tuple[tuple[str, str], ...],
        detail_rows: tuple[tuple[str, str], ...],
    ) -> None:
        self.compact_rows = compact_rows
        self.detail_rows = detail_rows
        self._refresh_static(".execution-summary", self._compact_text())
        self._refresh_static(".execution-details-content", self._details_text())

    def set_command(self, command: str) -> None:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command cannot be empty")
        self.command = command
        self._refresh_static(".execution-title", self._headline())
        if not self.compact_rows:
            self._refresh_static(".execution-summary", self.command)

    def apply_stage(self, stage: str, message: str | None = None) -> None:
        presentation = stage_presentation(stage)  # type: ignore[arg-type]
        self.stage = stage
        self._set_state(presentation.state)
        self._refresh_static(".execution-glyph", presentation.glyph)
        label = presentation.label if message is None else f"{presentation.label}"
        self._refresh_static(".execution-state-label", label)
        self._refresh_static(".execution-title", self._headline())
        if presentation.terminal:
            self._disable_cancel()

    def mark_cancelling(self) -> None:
        if self.cancel_requested:
            return
        self.cancel_requested = True
        self._set_state("cancelling")
        self._refresh_static(".execution-state-label", "Stopping")
        self._disable_cancel()

    def append_output(self, stream: str, text: str) -> None:
        del stream
        self._preview.append(text)
        self._refresh_static(".execution-output-content", self._preview.text())

    def finish(self, summary: str, audit_id: str | None = None) -> None:
        self.result_summary = summary
        self.audit_id = audit_id
        self._disable_cancel()
        self._refresh_static(".execution-title", self._headline())
        self._refresh_static(".execution-details-content", self._details_text())

    @on(Button.Pressed, ".execution-cancel")
    def request_cancel(self, event: Button.Pressed) -> None:
        event.stop()
        if self.cancel_requested:
            return
        self.post_message(self.CancelRequested(self.execution_id))

    @on(Button.Pressed, ".execution-details-toggle")
    def toggle_details(self, event: Button.Pressed) -> None:
        event.stop()
        expanded = not self.has_class("expanded")
        self.set_class(expanded, "expanded")
        event.button.label = "Hide" if expanded else "Details"

    def _set_state(self, state: str) -> None:
        if state not in EXECUTION_CARD_STATES:
            raise ValueError(f"Unsupported execution card state: {state}")
        self.remove_class(f"state-{self.state}")
        self.state = state
        self.add_class(f"state-{state}")

    def _disable_cancel(self) -> None:
        if not self.is_mounted:
            return
        try:
            self.query_one(".execution-cancel", Button).disabled = True
        except NoMatches:
            return

    def _refresh_static(self, selector: str, value: str) -> None:
        if not self.is_mounted:
            return
        try:
            self.query_one(selector, Static).update(value)
        except NoMatches:
            return

    def _headline(self) -> str:
        parts = [self.command]
        if self.result_summary:
            parts.append(self.result_summary)
        return "  ·  ".join(parts)

    def _compact_text(self) -> str:
        if not self.compact_rows:
            return self.command
        return "\n".join(f"{name}: {value}" for name, value in self.compact_rows)

    def _details_text(self) -> str:
        sections: list[str] = []
        if self.detail_rows:
            body = "\n".join(f"{name}: {value}" for name, value in self.detail_rows)
            sections.append(f"Execution contract\n{body}")
        if self.audit_id:
            sections.append(f"Audit\naudit id: {self.audit_id}")
        return "\n\n".join(sections)


PLAN_STEP_GLYPHS: dict[PlanStepStatus, str] = {
    "pending": "○",
    "in_progress": "▸",
    "done": "✓",
}
_PLAN_STEP_STYLES: dict[PlanStepStatus, str] = {
    "pending": "#a2a2a2",
    "in_progress": "#4da3ff bold",
    "done": "#707070",
}


class PlanCard(Vertical):
    def __init__(self, plan: Plan) -> None:
        if not isinstance(plan, Plan):
            raise TypeError("plan must be a Plan")

        self.plan = plan
        classes = "plan-card"
        if plan.is_complete:
            classes += " complete"
        super().__init__(classes=classes)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="plan-heading"):
            yield Static("◈", classes="plan-glyph", markup=False)
            yield Static("Plan", classes="plan-title", markup=False)
            yield Static("", classes="tool-spacer")
            yield Static(
                self._progress_label(),
                classes="plan-progress",
                markup=False,
            )

        yield Static(self._steps_text(), classes="plan-steps", markup=False)

    def update_plan(self, plan: Plan) -> None:
        if not isinstance(plan, Plan):
            raise TypeError("plan must be a Plan")

        self.plan = plan
        self.set_class(plan.is_complete, "complete")
        self._refresh_static(".plan-progress", self._progress_label())
        self._refresh_static(".plan-steps", self._steps_text())

    def _progress_label(self) -> str:
        return f"{self.plan.completed}/{self.plan.total}"

    def _steps_text(self) -> Text:
        text = Text()
        for index, step in enumerate(self.plan.steps):
            if index:
                text.append("\n")
            style = _PLAN_STEP_STYLES[step.status]
            text.append(f"{PLAN_STEP_GLYPHS[step.status]} ", style=style)
            text.append(step.title, style=style)
        return text

    def _refresh_static(self, selector: str, value: str | Text) -> None:
        if not self.is_mounted:
            return
        try:
            self.query_one(selector, Static).update(value)
        except NoMatches:
            return


# Kept as a compatibility alias for integrations importing the original widget.
ApprovalCard = ToolCallCard


_FOOTER_HINTS: Final = (
    ("ctrl+p", "sessions"),
    ("ctrl+a", "audit"),
    ("ctrl+e", "execution"),
    ("ctrl+q", "quit"),
)

_HINT_GAP: Final = "    "

_WORKSPACE_MAX_FRACTION: Final = 0.65


class StatusBar(Horizontal):
    """Persistent workspace and command footer."""

    def __init__(
        self,
        workspace: str,
        *,
        branch: str | None = None,
        version: str = "0.1.0",
        max_input_tokens: int = 0,
    ) -> None:
        self.workspace = workspace
        self.branch = branch
        self.version = version
        self.max_input_tokens = max_input_tokens
        self._conversation_active = False
        self._usage_tokens = 0
        self._execution_failure: str | None = None
        self._plan: Plan | None = None
        super().__init__(id="statusbar")

    def compose(self) -> ComposeResult:
        yield Static(
            self._workspace_label(),
            id="footer-workspace",
            markup=False,
        )
        yield Static("", classes="bar-spacer")
        yield Static(
            self._right_label(),
            id="footer-status",
            markup=False,
        )

    def on_resize(self) -> None:
        if self.is_mounted:
            self.query_one("#footer-status", Static).update(self._right_label())

    def set_conversation_active(self, active: bool) -> None:
        self._conversation_active = active
        if self.is_mounted:
            self.query_one("#footer-workspace", Static).update(
                self._workspace_label()
            )
            self.query_one("#footer-status", Static).update(self._right_label())

    def set_usage(self, usage: TokenUsage | None) -> None:
        if usage is None:
            return
        self._usage_tokens = usage.total_tokens or (
            usage.prompt_tokens + usage.completion_tokens
        )
        if self.is_mounted:
            self.query_one("#footer-status", Static).update(self._right_label())

    def set_execution_health(self, failure: str | None) -> None:
        if failure is not None and (
            not isinstance(failure, str) or not failure.strip()
        ):
            raise ValueError("failure must be non-empty text or None")
        self._execution_failure = failure
        if self.is_mounted:
            self.query_one("#footer-status", Static).update(self._right_label())

    def set_plan(self, plan: Plan | None) -> None:
        if plan is not None and not isinstance(plan, Plan):
            raise TypeError("plan must be a Plan or None")
        self._plan = plan
        if self.is_mounted:
            self.query_one("#footer-status", Static).update(self._right_label())

    def reset(self) -> None:
        self._usage_tokens = 0
        self._plan = None
        self.set_conversation_active(False)

    def _workspace_label(self) -> str:
        if not self._conversation_active and self.branch:
            return f"{self.workspace}:{self.branch}"
        return self.workspace

    def _right_label(self) -> Text:
        label = Text()
        if not self._conversation_active:
            if self._execution_failure is not None:
                label.append("shell unavailable", style="#ef6f78")
                label.append(" · ", style="#666666")
            label.append(self.version, style="#666666")
            return label

        if self._plan is not None:
            label.append(
                f"plan {self._plan.completed}/{self._plan.total}",
                style="#4da3ff" if not self._plan.is_complete else "#67c587",
            )
            label.append("    ")

        if self._usage_tokens:
            if self._usage_tokens >= 1000:
                token_label = f"{self._usage_tokens / 1000:.1f}K"
            else:
                token_label = str(self._usage_tokens)
            label.append(token_label, style="#777777")
            if self.max_input_tokens > 0:
                percentage = min(
                    100,
                    round(self._usage_tokens / self.max_input_tokens * 100),
                )
                label.append(f" ({percentage}%)", style="#666666")
            label.append("    ")
        self._append_hints(label)
        return label

    def _append_hints(self, label: Text) -> None:
        budget = self._hint_budget()
        for key, action in _FOOTER_HINTS:
            separator = _HINT_GAP if label.cell_len else ""
            width = len(separator) + len(key) + 1 + len(action)
            if budget >= 0 and label.cell_len + width > budget:
                return
            label.append(separator, style="#707070")
            label.append(key, style="#c8c8c8")
            label.append(f" {action}", style="#707070")

    def _hint_budget(self) -> int:
        available = self.content_size.width
        if available <= 0:
            return -1
        workspace = min(
            len(self._workspace_label()),
            int(available * _WORKSPACE_MAX_FRACTION),
        )
        return available - workspace - len(_HINT_GAP)
