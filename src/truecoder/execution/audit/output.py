from __future__ import annotations

import hashlib

from .models import MAX_AUDIT_PREVIEW_BYTES, OutputEvidence

_TRUNCATION_MARKER = "\n[... audit preview truncated ...]\n"


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

        self._stdout = _BoundedStream(max_preview_bytes)
        self._stderr = _BoundedStream(max_preview_bytes)
        self._complete = True

    def add_stdout(self, chunk: bytes) -> None:
        self._stdout.add(chunk)

    def add_stderr(self, chunk: bytes) -> None:
        self._stderr.add(chunk)

    def mark_incomplete(self) -> None:
        """Record that output collection stopped before both streams closed."""

        self._complete = False

    def snapshot(self) -> OutputEvidence:
        return OutputEvidence(
            stdout_sha256=self._stdout.digest,
            stderr_sha256=self._stderr.digest,
            stdout_bytes=self._stdout.total_bytes,
            stderr_bytes=self._stderr.total_bytes,
            stdout_preview=self._stdout.preview,
            stderr_preview=self._stderr.preview,
            stdout_truncated=self._stdout.truncated,
            stderr_truncated=self._stderr.truncated,
            complete=self._complete,
        )


class _BoundedStream:
    def __init__(self, max_preview_bytes: int) -> None:
        self._maximum = max_preview_bytes
        self._head_limit = max_preview_bytes // 2
        self._tail_limit = max_preview_bytes - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self._hash = hashlib.sha256()
        self.total_bytes = 0

    def add(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            raise TypeError("output chunks must be bytes")
        if not chunk:
            return

        self._hash.update(chunk)
        self.total_bytes += len(chunk)

        head_remaining = self._head_limit - len(self._head)
        if head_remaining > 0:
            self._head.extend(chunk[:head_remaining])
            chunk = chunk[head_remaining:]

        if self._tail_limit and chunk:
            self._tail.extend(chunk)
            overflow = len(self._tail) - self._tail_limit
            if overflow > 0:
                del self._tail[:overflow]

    @property
    def digest(self) -> str | None:
        if self.total_bytes == 0:
            return None
        return self._hash.hexdigest()

    @property
    def truncated(self) -> bool:
        return self.total_bytes > self._maximum

    @property
    def preview(self) -> str:
        if self.total_bytes == 0:
            return ""
        if not self.truncated:
            retained = bytes(self._head + self._tail)
            return retained.decode("utf-8", errors="ignore")

        marker = _marker_for_budget(self._maximum)
        retained_budget = self._maximum - len(marker)
        head_budget = retained_budget // 2
        tail_budget = retained_budget - head_budget
        preview = (
            bytes(self._head[:head_budget])
            + marker
            + (bytes(self._tail[-tail_budget:]) if tail_budget else b"")
        )
        return preview.decode("utf-8", errors="ignore")


def _marker_for_budget(maximum: int) -> bytes:
    marker = _TRUNCATION_MARKER.encode()
    if maximum >= len(marker) + 2:
        return marker
    if maximum >= 5:
        return b"..."
    return b""
