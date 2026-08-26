"""§4.5 ad-body rendering: stdlib ``HTMLParser`` only, and Greenhouse's double escaping."""

from __future__ import annotations

import pytest

from core.normalize.html import html_to_text, unescape_once


def test_blocks_become_line_breaks():
    html = "<h2>Who we are</h2><p>We build things.</p><ul><li>One</li><li>Two</li></ul>"
    assert html_to_text(html) == "Who we are\n\nWe build things.\n\nOne\n\nTwo"


def test_entities_are_resolved():
    assert html_to_text("<p>Salaries&nbsp;&amp; equity &lt;3</p>") == "Salaries & equity <3"


def test_script_and_style_bodies_are_dropped():
    html = "<p>Real text</p><script>var x = 1;</script><style>p{color:red}</style>"
    assert html_to_text(html) == "Real text"


def test_inline_tags_do_not_break_a_sentence():
    assert html_to_text("<p>We are <strong>hiring</strong> now</p>") == "We are hiring now"


def test_greenhouse_content_is_unescaped_once_then_parsed():
    escaped = "&lt;h2&gt;Who we are&lt;/h2&gt;&lt;p&gt;We ship.&lt;/p&gt;"
    assert html_to_text(unescape_once(escaped)) == "Who we are\n\nWe ship."


def test_plain_text_passes_through():
    assert html_to_text("Just a sentence.") == "Just a sentence."


@pytest.mark.parametrize("value", [None, "", "   ", "<p></p>", "<br>"])
def test_empty_renderings_are_none(value):
    assert html_to_text(value) is None


def test_unescape_once_is_none_safe():
    assert unescape_once(None) is None
    assert unescape_once("") == ""
    assert unescape_once("&amp;amp;") == "&amp;"  # exactly once, never twice
