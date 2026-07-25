from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Markdown, Static, TextArea

from truecoder.client.response import TokenUsage

ASCII_LOGO = (
    "█████ ████  █   █ █████  ███   ███  ████  █████ ████",
    " ░█░░░█░░░█ █░  █░█░░░░░█ ░░░ █ ░░█ █░░░█ █░░░░░█░░░█",
    "  █░░░████░░█░░ █░████░░█░ ░░░█░ ░█░█░░░█░████░░████░░",
    "  █░░ █░░█░ █░░ █░█░░░░ █░░   █░░ █░█░░ █░█░░░░ █░░█░",
    "  █░░ █░░░█░ ███ ░█████░ ███   ███ ░████ ░█████░█░░░█░",
)

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
            self.styles.height = min(8, max(3, visual_lines + 2))


class TopBar(Horizontal):
    """Compact session identity and context."""

    def __init__(
        self,
        model_name: str,
        workspace: str,
        mode_name: str = "AGENT",
    ) -> None:
        self.model_name = model_name
        self.workspace = Path(workspace).name or workspace
        self.mode_name = mode_name
        super().__init__(id="topbar")

    def compose(self) -> ComposeResult:
        yield Static("◆", id="brand-mark", markup=False)
        yield Static("TRUECODER", id="brand-name", markup=False)
        yield Static(self.mode_name, id="mode-badge", markup=False)
        yield Static("", classes="bar-spacer")
        yield Static(f"⌁ {self.workspace}", id="workspace-name", markup=False)
        yield Static(self.model_name, id="model-name", markup=False)


class EmptyState(Vertical):
    """Restrained glitch wordmark for an empty session."""

    def compose(self) -> ComposeResult:
        yield Static("\n".join(ASCII_LOGO), id="ascii-logo", markup=False)
        yield Static("◆  T R U E C O D E R", id="compact-logo", markup=False)
        yield Static(
            "A coding agent for the work in front of you.",
            id="splash-tagline",
            markup=False,
        )


class Composer(Vertical):
    """Compact, auto-growing prompt composer."""

    def __init__(self, model_name: str, workspace: str) -> None:
        short_workspace = Path(workspace).name or workspace
        self.context = f"{short_workspace}  ·  {model_name}  ·  Enter to start"
        super().__init__(id="composer-shell")

    def compose(self) -> ComposeResult:
        with Horizontal(id="composer-row"):
            yield PromptInput(
                id="prompt-input",
                placeholder="Ask TrueCoder to inspect, explain, or change code…",
                soft_wrap=True,
                show_line_numbers=False,
                tab_behavior="focus",
                compact=True,
            )
            yield Button("Send ↵", id="send-button", disabled=True)
        yield Static(
            "Enter send  ·  Shift+Enter newline",
            id="composer-help",
            markup=False,
        )
        yield Static(self.context, id="splash-context", markup=False)

    def set_busy(self, busy: bool) -> None:
        self.set_class(busy, "busy")
        self.query_one("#send-button", Button).label = "Busy" if busy else "Send ↵"


class ChatMessage(Vertical):
    """A user or assistant entry in the chronological timeline."""

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

        with Horizontal(classes="message-header"):
            yield Static(
                "◆" if self.role == "user" else "◇",
                classes="role-mark",
                markup=False,
            )
            yield Static(
                "YOU" if self.role == "user" else "TRUECODER",
                classes="role-label",
                markup=False,
            )
            yield Static("", classes="header-spacer")
            if self.role == "assistant":
                yield Static(
                    "thinking",
                    classes="message-state",
                    markup=False,
                )
            else:
                yield Static(
                    datetime.now(tz=timezone.utc).astimezone().strftime("%H:%M"),
                    classes="message-state",
                    markup=False,
                )

        yield Markdown(initial_content, classes="message-body")
        if self.role == "assistant":
            yield Static("", classes="message-footer", markup=False)

    async def append_delta(self, delta: str) -> None:
        if not self.content_text:
            self.query_one(".message-state", Static).update("● streaming")
            self.set_class(True, "streaming")
        self.content_text += delta
        await self.query_one(".message-body", Markdown).update(self.content_text)

    def finish(
        self,
        usage: TokenUsage | None,
        finish_reason: str | None = None,
    ) -> None:
        elapsed = monotonic() - self.started_at
        self.remove_class("streaming")
        self.add_class("completed")
        self.query_one(".message-state", Static).update("✓ completed")

        details = [f"{elapsed:.1f}s"]
        if usage is not None:
            details.append(f"{usage.completion_tokens:,} output tokens")
        if finish_reason and finish_reason != "stop":
            details.append(finish_reason.replace("_", " "))
        self.query_one(".message-footer", Static).update("  ·  ".join(details))

    async def show_error(self, error: str) -> None:
        self.remove_class("streaming")
        self.add_class("error")
        self.query_one(".message-state", Static).update("× failed")
        safe_error = error.replace("\\", "\\\\").replace("`", "\\`")
        self.content_text = f"**Request failed**\n\n{safe_error}"
        await self.query_one(".message-body", Markdown).update(self.content_text)
        self.query_one(".message-footer", Static).update(
            "Check the connection or API configuration, then try again."
        )

    async def show_cancelled(self) -> None:
        self.remove_class("streaming")
        self.add_class("cancelled")
        self.query_one(".message-state", Static).update("■ interrupted")
        if not self.content_text:
            self.content_text = "_Generation interrupted._"
            await self.query_one(".message-body", Markdown).update(self.content_text)


class ToolCallCard(Vertical):
    """Persistent tool activity with approval controls and expandable details."""

    def __init__(
        self,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | str,
        *,
        state: str = "queued",
    ) -> None:
        if state not in _TOOL_STATE_LABELS:
            raise ValueError(f"Unsupported tool-call state: {state}")

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
        self.started_at = monotonic()
        self.running_at = self.started_at if state == "running" else None

        classes = f"tool-call-card state-{state}"
        if self._is_risky:
            classes += " risky"
        if not self._target and not self._parameter_summary:
            classes += " no-summary"
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
            yield Static("", classes="tool-spacer")
            yield Button("Approve", classes="approval-approve")
            yield Button("Always allow", classes="approval-always")
            yield Button("Reject", classes="approval-reject")

        with VerticalScroll(classes="tool-details"):
            yield Static(self._details_text(), classes="tool-details-content")

    def set_awaiting_approval(self, arguments: dict[str, Any]) -> None:
        self.arguments = arguments
        self.running_at = None
        self.raw_arguments = json.dumps(
            arguments,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        self._refresh_summary()
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

    @on(Button.Pressed, ".tool-details-toggle")
    def toggle_details(self, event: Button.Pressed) -> None:
        event.stop()
        expanded = not self.has_class("expanded")
        self.set_class(expanded, "expanded")
        event.button.label = "Hide" if expanded else "Details"

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
        parts.append(self._elapsed_label())
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
        return ""

    def _elapsed_label(self) -> str:
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

    @staticmethod
    def _parse_arguments(arguments: dict[str, Any] | str) -> dict[str, Any]:
        if isinstance(arguments, dict):
            return arguments
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {"arguments": arguments}
        return parsed if isinstance(parsed, dict) else {"arguments": parsed}


# Kept as a compatibility alias for integrations importing the original widget.
ApprovalCard = ToolCallCard


class StatusBar(Horizontal):
    def __init__(self) -> None:
        super().__init__(id="statusbar")

    def compose(self) -> ComposeResult:
        yield Static("", classes="bar-spacer")
        yield Static(
            "Ctrl+L new chat  ·  Esc stop  ·  Ctrl+Q quit",
            id="shortcut-hint",
            markup=False,
        )
