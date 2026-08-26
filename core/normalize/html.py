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


#: Executable markup, stripped from `descriptionHtml` before it is published (V3 S23/S8).
#: The ad body is employer-written, reaches us over an unauthenticated public endpoint, and
#: buyers render it in dashboards and job boards — and Greenhouse's arrives `html.unescape`d
#: (§5.1), which is what turns an escaped payload in the provider's JSON into live markup
#: in ours. `html_to_text` already dropped `<script>` bodies for the *text* rendering only.
#:
#: Two passes, in this order — paired elements with their content first, then any orphan or
#: void tag left over. One pass with `.*?(?:</\1>|>)` does NOT work: the non-greedy run
#: matches the opening tag's own `>` and leaks both the body and the closing tag as text.
_ACTIVE_BLOCK = re.compile(
    r"(?is)<\s*(script|style|iframe|object|embed|form|template|noscript)\b[^>]*>"
    r".*?<\s*/\s*\1\s*>"
)
_ACTIVE_TAG = re.compile(
    r"(?is)<\s*/?\s*"
    r"(script|style|iframe|object|embed|form|link|meta|base|template|noscript)\b[^>]*>"
)
_EVENT_ATTR = re.compile(r"(?is)\s+on[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)")
_JS_URI = re.compile(
    r"(?is)((?:href|src|action|formaction)\s*=\s*[\"']?)\s*(?:javascript|vbscript|data):[^\"'\s>]*"
)


def sanitize_html(value: str | None) -> str | None:
    """Drop executable markup from an ad body. Formatting and ordinary links survive.

    ponytail: a regex sanitiser is not a parser. Two known ceilings — `[^>]*>` mis-cuts a
    tag whose attribute value contains a literal `>`, and nothing here defends against
    mutation XSS in a quirks-mode renderer. It removes the entire class of payloads that
    appear in real ad bodies (pasted tracking pixels, mail-merge junk, the occasional
    analytics snippet) at zero dependency cost. Upgrade to `nh3` (Rust `ammonia`, a real
    parser) if a buyer ever requires a formal guarantee; §9.3's three-dependency rule is
    why it is not there today. `input_schema.json` promises exactly this much and no more.
    """
    if not value:
        return value
    text = _ACTIVE_BLOCK.sub("", value)
    text = _ACTIVE_TAG.sub("", text)
    text = _EVENT_ATTR.sub("", text)
    return _JS_URI.sub(r"\1#", text)


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
