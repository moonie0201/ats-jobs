"""Contact redaction (SPEC v2 §4.5.3, §15.5) — email addresses and phone numbers out.

``redactContacts`` defaults to **on** (§4.1) because German- and Dutch-language ads on
Personio and Recruitee conventionally close with a named contact, which is where the
no-PII claim in §15.2 would otherwise be false.

Runs **before** salary parsing, so a redacted phone number cannot be read as a pay range
(§4.5.3). That is an improvement on the phone-number rejection gate, not a replacement.
"""

from __future__ import annotations

import re

PLACEHOLDER = "[redacted]"

_EMAIL = re.compile(
    r"""(?<![\w.+-])
        [A-Za-z0-9._%+-]+
        \s?(?:@|\(at\)|\[at\])\s?
        [A-Za-z0-9.-]+\.[A-Za-z]{2,24}
        (?![\w-])""",
    re.X,
)

_PHONE = re.compile(
    r"""
      (?<![\w.])(?:\+|00)\d[\d\s().\-/]{6,}\d            # international: +49 30 1234567
    | (?<![\w.$€£₹¥,])\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?![\d\w])   # NANP: (555) 123-4567
    """,
    re.X,
)

#: A phone number has at least this many digits; the guard keeps the loose international
#: pattern off "+1 more" and off long product codes.
_MIN_PHONE_DIGITS = 7


def _phone_sub(match: re.Match[str]) -> str:
    text = match.group(0)
    if sum(c.isdigit() for c in text) < _MIN_PHONE_DIGITS:
        return text
    return PLACEHOLDER


def redact_text(value: str | None) -> tuple[str | None, bool]:
    """``(text, something_was_removed)``. Emails first, then phone numbers.

    Only the matched spans are replaced, so HTML markup around them survives intact —
    ``<a href="mailto:[redacted]">`` is still a well-formed tag.
    """
    if not value:
        return value, False
    redacted = _EMAIL.sub(PLACEHOLDER, value)
    redacted = _PHONE.sub(_phone_sub, redacted)
    return redacted, redacted != value


def redact_description(
    description_html: str | None,
    description_text: str | None,
    enabled: bool = True,
) -> tuple[str | None, str | None, bool | None]:
    """Redact both renderings of one ad body.

    Returns ``(html, text, descriptionRedacted)``. The flag is ``None`` when redaction is
    switched off, ``True`` when something was removed and ``False`` when the body was
    scanned and came back clean.
    """
    if not enabled:
        return description_html, description_text, None
    new_html, html_hit = redact_text(description_html)
    new_text, text_hit = redact_text(description_text)
    return new_html, new_text, html_hit or text_hit
