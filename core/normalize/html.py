"""HTML -> plain text with the stdlib only (SPEC v2 §9.3: no bs4, no lxml).

Greenhouse's ``content`` arrives HTML-entity-escaped and must be unescaped exactly once
before it is treated as HTML (§5.1); everything else is already HTML.
"""

from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser

#: Elements whose boundaries are a line break in the text rendering.
_BLOCK = frozenset(
    """
    address article aside blockquote br dd div dl dt fieldset figcaption figure footer
    form h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table tbody td tfoot
    th thead tr ul
    """.split()
)

#: Elements whose *content* is not text at all.
_SKIP = frozenset({"script", "style", "head", "title", "noscript", "template"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _SKIP:
            self._skipping += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def unescape_once(value: str | None) -> str | None:
    """``html.unescape`` guarded for ``None`` — Greenhouse ``content`` needs it once."""
    return _html.unescape(value) if value else value


def html_to_text(value: str | None) -> str | None:
    """Render an ad body as plain text. ``None``/empty in, ``None`` out.

    Block elements become line breaks, ``<script>``/``<style>`` bodies are dropped,
    entities are resolved, runs of blank lines collapse to one.
    """
    if not value:
        return None
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts)
    text = text.replace(" ", " ").replace(" ", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None
