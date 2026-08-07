from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlparse

SYMBOL_KINDS: Final[dict[int, str]] = {
    1: "file",
    2: "module",
    3: "namespace",
    4: "package",
    5: "class",
    6: "method",
    7: "property",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    15: "string",
    16: "number",
    17: "boolean",
    18: "array",
    19: "object",
    20: "key",
    21: "null",
    22: "enum_member",
    23: "struct",
    24: "event",
    25: "operator",
    26: "type_parameter",
}

DIAGNOSTIC_SEVERITIES: Final[dict[int, str]] = {
    1: "error",
    2: "warning",
    3: "information",
    4: "hint",
}

LANGUAGE_IDS: Final[dict[str, str]] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".rs": "rust",
    ".go": "go",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
}


def path_to_uri(path: Path) -> str:
    return path.resolve().as_uri()


def uri_to_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    location = unquote(parsed.path)
    if not location:
        return None
    if len(location) > 2 and location[0] == "/" and location[2] == ":":
        location = location[1:]
    return Path(location)


def language_id_for(path: Path) -> str:
    return LANGUAGE_IDS.get(path.suffix.lower(), "plaintext")


def display_path(uri: str, root: Path) -> str:
    path = uri_to_path(uri)
    if path is None:
        return uri
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


@dataclass(frozen=True, slots=True)
class Position:
    line: int
    character: int

    @property
    def one_based_line(self) -> int:
        return self.line + 1


@dataclass(frozen=True, slots=True)
class Range:
    start: Position
    end: Position


@dataclass(frozen=True, slots=True)
class Location:
    path: str
    range: Range

    @property
    def label(self) -> str:
        return f"{self.path}:{self.range.start.one_based_line}"


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    name: str
    kind: str
    location: Location
    container: str = ""


@dataclass(frozen=True, slots=True)
class Diagnostic:
    path: str
    range: Range
    severity: str
    message: str
    source: str = ""


def _integer(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_position(payload: Any) -> Position:
    if not isinstance(payload, dict):
        return Position(line=0, character=0)
    return Position(
        line=_integer(payload.get("line")),
        character=_integer(payload.get("character")),
    )


def parse_range(payload: Any) -> Range:
    if not isinstance(payload, dict):
        return Range(start=Position(0, 0), end=Position(0, 0))
    return Range(
        start=parse_position(payload.get("start")),
        end=parse_position(payload.get("end")),
    )


def parse_locations(payload: Any, root: Path) -> tuple[Location, ...]:
    if payload is None:
        return ()

    entries = payload if isinstance(payload, list) else [payload]
    locations: list[Location] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        uri = entry.get("uri") or entry.get("targetUri")
        if not isinstance(uri, str):
            continue
        raw_range = (
            entry.get("range")
            or entry.get("targetSelectionRange")
            or entry.get("targetRange")
        )
        locations.append(
            Location(path=display_path(uri, root), range=parse_range(raw_range))
        )
    return tuple(locations)


def parse_symbols(
    payload: Any,
    root: Path,
    *,
    default_uri: str | None = None,
    container: str = "",
) -> tuple[SymbolInfo, ...]:
    if not isinstance(payload, list):
        return ()

    symbols: list[SymbolInfo] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue

        name = entry.get("name")
        if not isinstance(name, str):
            continue

        kind = SYMBOL_KINDS.get(_integer(entry.get("kind")), "unknown")
        raw_location = entry.get("location")

        if isinstance(raw_location, dict):
            uri = raw_location.get("uri")
            raw_range = raw_location.get("range")
        else:
            uri = default_uri
            raw_range = entry.get("selectionRange") or entry.get("range")

        if not isinstance(uri, str):
            continue

        symbols.append(
            SymbolInfo(
                name=name,
                kind=kind,
                location=Location(
                    path=display_path(uri, root),
                    range=parse_range(raw_range),
                ),
                container=str(entry.get("containerName") or container),
            )
        )

        children = entry.get("children")
        if isinstance(children, list):
            symbols.extend(
                parse_symbols(
                    children,
                    root,
                    default_uri=uri,
                    container=name,
                )
            )

    return tuple(symbols)


def parse_diagnostics(payload: Any, uri: str, root: Path) -> tuple[Diagnostic, ...]:
    if not isinstance(payload, list):
        return ()

    path = display_path(uri, root)
    diagnostics: list[Diagnostic] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        if not isinstance(message, str):
            continue
        diagnostics.append(
            Diagnostic(
                path=path,
                range=parse_range(entry.get("range")),
                severity=DIAGNOSTIC_SEVERITIES.get(
                    _integer(entry.get("severity")),
                    "information",
                ),
                message=message,
                source=str(entry.get("source") or ""),
            )
        )
    return tuple(diagnostics)


def parse_hover(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, list):
        return "\n\n".join(part for part in (parse_hover(item) for item in payload) if part)
    if not isinstance(payload, dict):
        return ""

    contents = payload.get("contents", payload)
    if contents is not payload:
        return parse_hover(contents)

    value = payload.get("value")
    return value.strip() if isinstance(value, str) else ""
