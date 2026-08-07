from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final

MAX_EXTRACTED_CHARACTERS: Final = 40_000

_DISCARDED_ELEMENTS: Final = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "canvas",
        "iframe",
        "object",
        "embed",
        "head",
    }
)
_BLOCK_ELEMENTS: Final = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "dd",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)

_BLANK_RUN = re.compile(r"\n{3,}")
_SPACE_RUN = re.compile(r"[^\S\n]{2,}")
_TRAILING_SPACE = re.compile(r"[^\S\n]+\n")
_ANY_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    title: str
    text: str
    truncated: bool


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._discard_depth = 0
        self._pre_depth = 0
        self._title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        if tag in _DISCARDED_ELEMENTS:
            self._discard_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "pre":
            self._pre_depth += 1
        if tag in _BLOCK_ELEMENTS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        del attrs
        if tag in _BLOCK_ELEMENTS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DISCARDED_ELEMENTS:
            self._discard_depth = max(0, self._discard_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
        if tag in _BLOCK_ELEMENTS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._discard_depth:
            return
        if self._pre_depth:
            self._chunks.append(data)
            return
        # A newline in the source is ordinary whitespace in HTML. Line breaks
        # come from block boundaries, which are appended as their own chunks.
        self._chunks.append(_ANY_WHITESPACE.sub(" ", data))

    @property
    def title(self) -> str:
        return _SPACE_RUN.sub(" ", " ".join(self._title_parts).strip())

    @property
    def text(self) -> str:
        return "".join(self._chunks)


def _tidy(text: str) -> str:
    collapsed = text.replace("\r\n", "\n").replace("\r", "\n")
    collapsed = _TRAILING_SPACE.sub("\n", collapsed)
    collapsed = _BLANK_RUN.sub("\n\n", collapsed)
    return collapsed.strip()


def extract_text(
    markup: str,
    *,
    max_characters: int = MAX_EXTRACTED_CHARACTERS,
) -> ExtractedDocument:
    if not isinstance(markup, str):
        raise TypeError("markup must be text")
    if isinstance(max_characters, bool) or not isinstance(max_characters, int):
        raise TypeError("max_characters must be an integer")
    if max_characters < 1:
        raise ValueError("max_characters must be at least one")

    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except (AssertionError, ValueError):
        # Malformed markup should degrade to whatever was parsed, not fail.
        pass

    text = _tidy(parser.text)
    truncated = len(text) > max_characters
    if truncated:
        text = text[:max_characters].rstrip()

    return ExtractedDocument(title=parser.title, text=text, truncated=truncated)


def extract_plain_text(
    body: str,
    *,
    max_characters: int = MAX_EXTRACTED_CHARACTERS,
) -> ExtractedDocument:
    if not isinstance(body, str):
        raise TypeError("body must be text")

    text = _tidy(body)
    truncated = len(text) > max_characters
    if truncated:
        text = text[:max_characters].rstrip()

    return ExtractedDocument(title="", text=text, truncated=truncated)
