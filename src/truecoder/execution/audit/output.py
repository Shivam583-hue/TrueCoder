from __future__ import annotations

from ..output import BoundedByteStream, render_snapshot_text
from .models import MAX_AUDIT_PREVIEW_BYTES, OutputEvidence


class BoundedOutputEvidence:
    """Hash complete byte streams while retaining bounded first/last previews."""

    def __init__(self, max_preview_bytes: int = 128 * 1024) -> None:
        if isinstance(max_preview_bytes, bool) or not isinstance(
            max_preview_bytes,
            int,
        ):
            raise TypeError("max_preview_bytes must be an integer")
        if max_preview_bytes < 0:
            raise ValueError("max_preview_bytes must not be negative")
        if max_preview_bytes > MAX_AUDIT_PREVIEW_BYTES:
            raise ValueError(
                f"max_preview_bytes must not exceed {MAX_AUDIT_PREVIEW_BYTES}"
            )

        self._maximum = max_preview_bytes
        self._stdout = BoundedByteStream(max_preview_bytes)
        self._stderr = BoundedByteStream(max_preview_bytes)
        self._complete = True

    def add_stdout(self, chunk: bytes) -> None:
        self._stdout.feed(chunk)

    def add_stderr(self, chunk: bytes) -> None:
        self._stderr.feed(chunk)

    def mark_incomplete(self) -> None:
        """Record that output collection stopped before both streams closed."""

        self._complete = False

    def snapshot(self) -> OutputEvidence:
        stdout = self._stdout.snapshot()
        stderr = self._stderr.snapshot()
        stdout_preview, stdout_truncated = render_snapshot_text(
            stdout,
            self._maximum,
        )
        stderr_preview, stderr_truncated = render_snapshot_text(
            stderr,
            self._maximum,
        )
        return OutputEvidence(
            stdout_sha256=stdout.sha256,
            stderr_sha256=stderr.sha256,
            stdout_bytes=stdout.total_bytes,
            stderr_bytes=stderr.total_bytes,
            stdout_preview=stdout_preview,
            stderr_preview=stderr_preview,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            complete=self._complete,
        )
