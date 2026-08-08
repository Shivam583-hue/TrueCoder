from __future__ import annotations

from difflib import SequenceMatcher

from truecoder.mutation.models import (
    DIFF_CONTEXT_LINES,
    MAX_DIFF_LINE_LENGTH,
    MAX_DIFF_LINES,
    DiffHunk,
    DiffLine,
    FileDiff,
    MutationKind,
)

_TRUNCATION_MARK = "…"


def _display(text: str) -> str:
    if len(text) <= MAX_DIFF_LINE_LENGTH:
        return text
    return text[:MAX_DIFF_LINE_LENGTH] + _TRUNCATION_MARK


def _ends_with_newline(text: str) -> bool:
    return text.endswith(("\n", "\r"))


def build_file_diff(
    path: str,
    before: str,
    after: str,
    *,
    kind: MutationKind,
    context_lines: int = DIFF_CONTEXT_LINES,
    max_lines: int = MAX_DIFF_LINES,
) -> FileDiff:
    if not isinstance(before, str) or not isinstance(after, str):
        raise TypeError("Diff inputs must be text.")
    if isinstance(context_lines, bool) or not isinstance(context_lines, int):
        raise TypeError("context_lines must be an integer.")
    if context_lines < 0:
        raise ValueError("context_lines cannot be negative.")
    if isinstance(max_lines, bool) or not isinstance(max_lines, int):
        raise TypeError("max_lines must be an integer.")
    if max_lines < 1:
        raise ValueError("max_lines must be at least one.")

    before_lines = before.splitlines()
    after_lines = after.splitlines()

    # autojunk treats lines repeated across a long file as noise, which for
    # source code means closing braces and blank lines stop matching.
    matcher = SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)

    added = 0
    removed = 0
    for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += before_end - before_start
        if tag in ("replace", "insert"):
            added += after_end - after_start

    hunks: list[DiffHunk] = []
    budget = max_lines
    truncated = False

    for group in matcher.get_grouped_opcodes(context_lines):
        if budget <= 0:
            truncated = True
            break

        lines: list[DiffLine] = []
        for tag, before_start, before_end, after_start, after_end in group:
            if tag == "equal":
                for offset in range(before_end - before_start):
                    lines.append(
                        DiffLine(
                            kind="context",
                            text=_display(before_lines[before_start + offset]),
                            before_number=before_start + offset + 1,
                            after_number=after_start + offset + 1,
                        )
                    )
                continue

            if tag in ("replace", "delete"):
                for index in range(before_start, before_end):
                    lines.append(
                        DiffLine(
                            kind="removed",
                            text=_display(before_lines[index]),
                            before_number=index + 1,
                            after_number=None,
                        )
                    )
            if tag in ("replace", "insert"):
                for index in range(after_start, after_end):
                    lines.append(
                        DiffLine(
                            kind="added",
                            text=_display(after_lines[index]),
                            before_number=None,
                            after_number=index + 1,
                        )
                    )

        if not lines:
            continue

        if len(lines) > budget:
            lines = lines[:budget]
            truncated = True

        budget -= len(lines)
        first_before = group[0][1]
        last_before = group[-1][2]
        first_after = group[0][3]
        last_after = group[-1][4]
        hunks.append(
            DiffHunk(
                before_start=first_before + 1 if last_before > first_before else 0,
                before_count=last_before - first_before,
                after_start=first_after + 1 if last_after > first_after else 0,
                after_count=last_after - first_after,
                lines=tuple(lines),
            )
        )

    newline_changed = (
        bool(before)
        and bool(after)
        and (_ends_with_newline(before) != _ends_with_newline(after))
    )

    # Equal split lines mean no hunks, so without this any difference that
    # survives splitlines - CRLF against LF above all - would render as an
    # unchanged file while the write still rewrites every line.
    line_endings_changed = (
        before != after and before_lines == after_lines and not newline_changed
    )

    return FileDiff(
        path=path,
        kind=kind,
        hunks=tuple(hunks),
        added=added,
        removed=removed,
        truncated=truncated,
        newline_changed=newline_changed,
        line_endings_changed=line_endings_changed,
    )
