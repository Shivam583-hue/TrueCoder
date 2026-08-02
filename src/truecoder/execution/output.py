from __future__ import annotations

import codecs
import hashlib
from dataclasses import dataclass, field
from typing import Final, Literal, TypeAlias

from .models import ExecutionLimits

StreamName: TypeAlias = Literal["stdout", "stderr"]

REDACTION_MARKER: Final = "[REDACTED]"
TRUNCATION_MARKER: Final = "\n[... output truncated ...]\n"
MAX_TERMINAL_SEQUENCE_CHARS: Final = 4096
MAX_REDACTION_VALUES: Final = 256
MAX_REDACTION_VALUE_CHARS: Final = 4096


@dataclass(frozen=True, slots=True)
class ByteStreamSnapshot:
    total_bytes: int
    sha256: str | None
    retention_capacity: int
    head: bytes = field(repr=False)
    tail: bytes = field(repr=False)
    finished: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("total_bytes", self.total_bytes),
            ("retention_capacity", self.retention_capacity),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must not be negative")
        if self.sha256 is not None and (
            not isinstance(self.sha256, str) or len(self.sha256) != 64
        ):
            raise ValueError("sha256 must be a 64-character digest or None")
        if not isinstance(self.head, bytes) or not isinstance(self.tail, bytes):
            raise TypeError("head and tail must be bytes")
        if len(self.head) + len(self.tail) > self.retention_capacity:
            raise ValueError("retained bytes exceed retention_capacity")
        if not isinstance(self.finished, bool):
            raise TypeError("finished must be a boolean")

    @property
    def retained_bytes(self) -> int:
        return len(self.head) + len(self.tail)

    def preview_bytes(self, budget: int) -> bytes:
        _validate_budget(budget, self.retention_capacity)
        if self.total_bytes == 0 or budget == 0:
            return b""
        if self.total_bytes <= budget:
            return self.head + self.tail

        marker = _marker_for_budget(budget)
        retained_budget = budget - len(marker)
        head_budget = retained_budget // 2
        tail_budget = retained_budget - head_budget
        return (
            self.head[:head_budget]
            + marker
            + (self.tail[-tail_budget:] if tail_budget else b"")
        )


@dataclass(frozen=True, slots=True)
class OutputUpdate:
    stream: StreamName
    text: str
    combined_bytes: int
    limit_exceeded: bool
    newly_exceeded: bool
    stream_closed: bool = False

    def __post_init__(self) -> None:
        if self.stream not in {"stdout", "stderr"}:
            raise ValueError(f"unknown output stream: {self.stream!r}")
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        if isinstance(self.combined_bytes, bool) or not isinstance(
            self.combined_bytes, int
        ):
            raise TypeError("combined_bytes must be an integer")
        if self.combined_bytes < 0:
            raise ValueError("combined_bytes must not be negative")
        for name, value in (
            ("limit_exceeded", self.limit_exceeded),
            ("newly_exceeded", self.newly_exceeded),
            ("stream_closed", self.stream_closed),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be a boolean")
        if self.newly_exceeded and not self.limit_exceeded:
            raise ValueError("newly_exceeded requires limit_exceeded")


@dataclass(frozen=True, slots=True)
class StreamOutput:
    text: str
    byte_count: int
    sha256: str | None
    truncated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        if isinstance(self.byte_count, bool) or not isinstance(
            self.byte_count,
            int,
        ):
            raise TypeError("byte_count must be an integer")
        if self.byte_count < 0:
            raise ValueError("byte_count must not be negative")
        if self.sha256 is not None and (
            not isinstance(self.sha256, str) or len(self.sha256) != 64
        ):
            raise ValueError("sha256 must be a 64-character digest or None")
        if not isinstance(self.truncated, bool):
            raise TypeError("truncated must be a boolean")


@dataclass(frozen=True, slots=True)
class CollectedOutput:
    stdout: StreamOutput
    stderr: StreamOutput
    complete: bool
    output_limit_exceeded: bool
    retained_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.stdout, StreamOutput):
            raise TypeError("stdout must be a StreamOutput")
        if not isinstance(self.stderr, StreamOutput):
            raise TypeError("stderr must be a StreamOutput")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be a boolean")
        if not isinstance(self.output_limit_exceeded, bool):
            raise TypeError("output_limit_exceeded must be a boolean")
        if isinstance(self.retained_bytes, bool) or not isinstance(
            self.retained_bytes,
            int,
        ):
            raise TypeError("retained_bytes must be an integer")
        if self.retained_bytes < 0:
            raise ValueError("retained_bytes must not be negative")


class BoundedByteStream:
    """Count and hash all bytes while retaining fixed first and last windows."""

    def __init__(self, retention_bytes: int) -> None:
        _validate_nonnegative_int(retention_bytes, "retention_bytes")
        self._retention = retention_bytes
        self._head_limit = retention_bytes // 2
        self._tail_limit = retention_bytes - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self._hash = hashlib.sha256()
        self._total_bytes = 0
        self._finished = False

    def feed(self, chunk: bytes) -> None:
        if self._finished:
            raise RuntimeError("cannot feed a finished byte stream")
        if not isinstance(chunk, bytes):
            raise TypeError("output chunks must be bytes")
        if not chunk:
            return

        self._hash.update(chunk)
        self._total_bytes += len(chunk)

        head_remaining = self._head_limit - len(self._head)
        if head_remaining > 0:
            self._head.extend(chunk[:head_remaining])
            chunk = chunk[head_remaining:]

        if self._tail_limit and chunk:
            self._tail.extend(chunk)
            overflow = len(self._tail) - self._tail_limit
            if overflow > 0:
                del self._tail[:overflow]

    def finish(self) -> None:
        self._finished = True

    @property
    def retained_bytes(self) -> int:
        return len(self._head) + len(self._tail)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def snapshot(self) -> ByteStreamSnapshot:
        return ByteStreamSnapshot(
            total_bytes=self._total_bytes,
            sha256=self._hash.hexdigest() if self._total_bytes else None,
            retention_capacity=self._retention,
            head=bytes(self._head),
            tail=bytes(self._tail),
            finished=self._finished,
        )


class TerminalSanitizer:
    """Incrementally remove terminal control sequences and unsafe controls."""

    def __init__(
        self,
        max_sequence_chars: int = MAX_TERMINAL_SEQUENCE_CHARS,
    ) -> None:
        _validate_nonnegative_int(max_sequence_chars, "max_sequence_chars")
        if max_sequence_chars == 0:
            raise ValueError("max_sequence_chars must be greater than zero")
        self._maximum = max_sequence_chars
        self._state = "text"
        self._sequence_chars = 0
        self._pending_carriage_return = False
        self._finished = False

    def feed(self, text: str, *, final: bool = False) -> str:
        if self._finished:
            raise RuntimeError("cannot feed a finished terminal sanitizer")
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not isinstance(final, bool):
            raise TypeError("final must be a boolean")

        output: list[str] = []
        for character in text:
            self._consume(character, output)
        if final:
            if self._pending_carriage_return:
                output.append("\n")
            self._pending_carriage_return = False
            self._state = "text"
            self._sequence_chars = 0
            self._finished = True
        return "".join(output)

    def _consume(self, character: str, output: list[str]) -> None:
        if self._state == "text":
            self._consume_text(character, output)
            return
        if self._state == "escape":
            if character == "[":
                self._begin_sequence("csi")
            elif character == "]":
                self._begin_sequence("osc")
            else:
                self._state = "text"
            return
        if self._state == "csi":
            self._sequence_chars += 1
            if 0x40 <= ord(character) <= 0x7E or self._sequence_chars > self._maximum:
                self._state = "text"
                self._sequence_chars = 0
            return
        if self._state == "osc":
            self._sequence_chars += 1
            if character == "\x07":
                self._state = "text"
                self._sequence_chars = 0
            elif character == "\x1b":
                self._state = "osc-escape"
            elif self._sequence_chars > self._maximum:
                self._state = "text"
                self._sequence_chars = 0
            return
        if self._state == "osc-escape":
            if character in {"\\", "\x07"}:
                self._state = "text"
                self._sequence_chars = 0
            else:
                self._state = "osc"
                self._sequence_chars += 1

    def _consume_text(self, character: str, output: list[str]) -> None:
        if self._pending_carriage_return:
            output.append("\n")
            self._pending_carriage_return = False
            if character == "\n":
                return

        if character == "\r":
            self._pending_carriage_return = True
            return
        if character in {"\x1b", "\x9b", "\x9d"}:
            if character == "\x1b":
                self._state = "escape"
            elif character == "\x9b":
                self._begin_sequence("csi")
            else:
                self._begin_sequence("osc")
            return
        codepoint = ord(character)
        if character in {"\n", "\t"} or (
            codepoint >= 0x20 and codepoint != 0x7F and not 0x80 <= codepoint <= 0x9F
        ):
            output.append(character)

    def _begin_sequence(self, state: str) -> None:
        self._state = state
        self._sequence_chars = 0


class StreamingValueRedactor:
    """Redact exact values without leaking matches divided across chunks."""

    def __init__(
        self,
        values: tuple[str, ...] = (),
        *,
        replacement: str = REDACTION_MARKER,
    ) -> None:
        if not isinstance(values, tuple):
            raise TypeError("values must be a tuple")
        if len(values) > MAX_REDACTION_VALUES:
            raise ValueError("too many redaction values")
        if not isinstance(replacement, str) or not replacement:
            raise ValueError("replacement must be a non-empty string")

        unique: set[str] = set()
        for index, value in enumerate(values):
            if not isinstance(value, str):
                raise TypeError(f"values[{index}] must be a string")
            if not value:
                raise ValueError(f"values[{index}] must not be empty")
            if len(value) > MAX_REDACTION_VALUE_CHARS:
                raise ValueError(f"values[{index}] is too long")
            unique.add(value)
        self._values = tuple(sorted(unique, key=lambda value: (-len(value), value)))
        self._replacement = replacement
        self._buffer = ""
        self._finished = False

    @property
    def pending_characters(self) -> int:
        return len(self._buffer)

    def feed(self, text: str, *, final: bool = False) -> str:
        if self._finished:
            raise RuntimeError("cannot feed a finished value redactor")
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not isinstance(final, bool):
            raise TypeError("final must be a boolean")

        self._buffer += text
        output: list[str] = []
        while self._buffer:
            match = self._earliest_match()
            if match is not None:
                index, value = match
                output.append(self._buffer[:index])
                output.append(self._replacement)
                self._buffer = self._buffer[index + len(value) :]
                continue
            if final or not self._values:
                output.append(self._buffer)
                self._buffer = ""
                break

            suffix_length = self._longest_pending_prefix()
            if suffix_length:
                output.append(self._buffer[:-suffix_length])
                self._buffer = self._buffer[-suffix_length:]
            else:
                output.append(self._buffer)
                self._buffer = ""
            break

        if final:
            self._finished = True
        return "".join(output)

    def _earliest_match(self) -> tuple[int, str] | None:
        matches = (
            (index, -len(value), value)
            for value in self._values
            if (index := self._buffer.find(value)) >= 0
        )
        best = min(matches, default=None)
        if best is None:
            return None
        return best[0], best[2]

    def _longest_pending_prefix(self) -> int:
        maximum = min(
            len(self._buffer),
            max((len(value) for value in self._values), default=0) - 1,
        )
        for size in range(maximum, 0, -1):
            suffix = self._buffer[-size:]
            if any(value.startswith(suffix) for value in self._values):
                return size
        return 0


class OutputCollector:
    """Pure two-stream output accounting, decoding, and bounded retention."""

    def __init__(
        self,
        limits: ExecutionLimits,
        *,
        redaction_values: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(limits, ExecutionLimits):
            raise TypeError("limits must be an ExecutionLimits")
        self._limits = limits
        self._stdout = _StreamState(limits.max_return_bytes, redaction_values)
        self._stderr = _StreamState(limits.max_return_bytes, redaction_values)
        self._limit_exceeded = False

    def feed_stdout(self, chunk: bytes) -> OutputUpdate:
        return self._feed("stdout", self._stdout, chunk)

    def feed_stderr(self, chunk: bytes) -> OutputUpdate:
        return self._feed("stderr", self._stderr, chunk)

    def close_stdout(self) -> OutputUpdate:
        return self._close("stdout", self._stdout)

    def close_stderr(self) -> OutputUpdate:
        return self._close("stderr", self._stderr)

    @property
    def combined_bytes(self) -> int:
        return self._stdout.raw.total_bytes + self._stderr.raw.total_bytes

    @property
    def retained_bytes(self) -> int:
        return self._stdout.raw.retained_bytes + self._stderr.raw.retained_bytes

    @property
    def pending_text_characters(self) -> int:
        return (
            self._stdout.pending_text_characters + self._stderr.pending_text_characters
        )

    def snapshot(self) -> CollectedOutput:
        stdout_snapshot = self._stdout.raw.snapshot()
        stderr_snapshot = self._stderr.raw.snapshot()
        stdout_budget, stderr_budget = _allocate_budgets(
            self._limits.max_return_bytes,
            stdout_snapshot.total_bytes,
            stderr_snapshot.total_bytes,
        )
        stdout_text, stdout_truncated = render_snapshot_text(
            stdout_snapshot,
            stdout_budget,
            redaction_values=self._stdout.redaction_values,
        )
        stderr_text, stderr_truncated = render_snapshot_text(
            stderr_snapshot,
            stderr_budget,
            redaction_values=self._stderr.redaction_values,
        )
        return CollectedOutput(
            stdout=StreamOutput(
                text=stdout_text,
                byte_count=stdout_snapshot.total_bytes,
                sha256=stdout_snapshot.sha256,
                truncated=stdout_truncated,
            ),
            stderr=StreamOutput(
                text=stderr_text,
                byte_count=stderr_snapshot.total_bytes,
                sha256=stderr_snapshot.sha256,
                truncated=stderr_truncated,
            ),
            complete=self._stdout.closed and self._stderr.closed,
            output_limit_exceeded=self._limit_exceeded,
            retained_bytes=self.retained_bytes,
        )

    def _feed(
        self,
        stream: StreamName,
        state: _StreamState,
        chunk: bytes,
    ) -> OutputUpdate:
        text = state.feed(chunk)
        newly_exceeded = (
            not self._limit_exceeded
            and self.combined_bytes > self._limits.max_output_bytes
        )
        if newly_exceeded:
            self._limit_exceeded = True
        return OutputUpdate(
            stream=stream,
            text=text,
            combined_bytes=self.combined_bytes,
            limit_exceeded=self._limit_exceeded,
            newly_exceeded=newly_exceeded,
        )

    def _close(
        self,
        stream: StreamName,
        state: _StreamState,
    ) -> OutputUpdate:
        text = state.close()
        return OutputUpdate(
            stream=stream,
            text=text,
            combined_bytes=self.combined_bytes,
            limit_exceeded=self._limit_exceeded,
            newly_exceeded=False,
            stream_closed=True,
        )


class _StreamState:
    def __init__(
        self,
        retention_bytes: int,
        redaction_values: tuple[str, ...],
    ) -> None:
        self.raw = BoundedByteStream(retention_bytes)
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.sanitizer = TerminalSanitizer()
        self.redactor = StreamingValueRedactor(redaction_values)
        self.redaction_values = redaction_values
        self.closed = False

    @property
    def pending_text_characters(self) -> int:
        undecoded_bytes, _flag = self.decoder.getstate()
        return len(undecoded_bytes) + self.redactor.pending_characters

    def feed(self, chunk: bytes) -> str:
        if self.closed:
            raise RuntimeError("cannot feed a closed output stream")
        self.raw.feed(chunk)
        decoded = self.decoder.decode(chunk, final=False)
        sanitized = self.sanitizer.feed(decoded)
        return self.redactor.feed(sanitized)

    def close(self) -> str:
        if self.closed:
            raise RuntimeError("output stream is already closed")
        decoded = self.decoder.decode(b"", final=True)
        sanitized = self.sanitizer.feed(decoded, final=True)
        redacted = self.redactor.feed(sanitized, final=True)
        self.raw.finish()
        self.closed = True
        return redacted


def render_snapshot_text(
    snapshot: ByteStreamSnapshot,
    budget: int,
    *,
    redaction_values: tuple[str, ...] = (),
) -> tuple[str, bool]:
    if not isinstance(snapshot, ByteStreamSnapshot):
        raise TypeError("snapshot must be a ByteStreamSnapshot")
    raw = snapshot.preview_bytes(budget)
    decoded = raw.decode("utf-8", errors="replace")
    sanitized = TerminalSanitizer().feed(decoded, final=True)
    redacted = StreamingValueRedactor(redaction_values).feed(
        sanitized,
        final=True,
    )
    bounded, text_was_truncated = _bound_text(redacted, budget)
    return bounded, snapshot.total_bytes > budget or text_was_truncated


def _allocate_budgets(
    total_budget: int,
    stdout_bytes: int,
    stderr_bytes: int,
) -> tuple[int, int]:
    _validate_nonnegative_int(total_budget, "total_budget")
    _validate_nonnegative_int(stdout_bytes, "stdout_bytes")
    _validate_nonnegative_int(stderr_bytes, "stderr_bytes")
    combined = stdout_bytes + stderr_bytes
    if combined == 0:
        return 0, 0
    if stdout_bytes == 0:
        return 0, total_budget
    if stderr_bytes == 0:
        return total_budget, 0
    stdout_budget = total_budget * stdout_bytes // combined
    return stdout_budget, total_budget - stdout_budget


def _bound_text(text: str, budget: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text, False
    if budget == 0:
        return "", True

    marker = _text_marker_for_budget(budget)
    remaining = budget - len(marker.encode("utf-8"))
    head_budget = remaining // 2
    tail_budget = remaining - head_budget
    head = _utf8_prefix(text, head_budget)
    tail = _utf8_suffix(text, tail_budget)
    return head + marker + tail, True


def _utf8_prefix(text: str, budget: int) -> str:
    output: list[str] = []
    used = 0
    for character in text:
        size = len(character.encode("utf-8"))
        if used + size > budget:
            break
        output.append(character)
        used += size
    return "".join(output)


def _utf8_suffix(text: str, budget: int) -> str:
    output: list[str] = []
    used = 0
    for character in reversed(text):
        size = len(character.encode("utf-8"))
        if used + size > budget:
            break
        output.append(character)
        used += size
    return "".join(reversed(output))


def _marker_for_budget(budget: int) -> bytes:
    marker = TRUNCATION_MARKER.encode("utf-8")
    if budget >= len(marker) + 2:
        return marker
    if budget >= 5:
        return b"..."
    return b""


def _text_marker_for_budget(budget: int) -> str:
    marker = TRUNCATION_MARKER
    if budget >= len(marker.encode("utf-8")) + 2:
        return marker
    if budget >= 5:
        return "..."
    return ""


def _validate_budget(budget: int, maximum: int) -> None:
    _validate_nonnegative_int(budget, "budget")
    if budget > maximum:
        raise ValueError("budget cannot exceed retained capacity")


def _validate_nonnegative_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
