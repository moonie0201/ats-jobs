"""Contact redaction (SPEC v2 §4.5.3, §15.5) — email addresses and phone numbers out.

``redactContacts`` defaults to **on** (§4.1) because German- and Dutch-language ads on
Personio and Recruitee conventionally close with a named contact.

**What this does not do:** it removes contact *channels*, not *identity*. A name in running
prose has no pattern to match, so a named contact person survives with their address gone.
`PRIVACY.md` says so in those words, and the Store listing must not claim otherwise.

Runs **before** salary parsing, so a redacted phone number cannot be read as a pay range
(§4.5.3). That is an improvement on the phone-number rejection gate, not a replacement.
"""

from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = "[redacted]"

#: Whitespace is allowed only around the *obfuscated* separators, where it is part of the
#: convention (``jane (at) acme.com``). A literal ``@`` must touch its local part: with
#: ``\s?`` on both sides, Cohere's live anti-fraud line "communications … coming from an
#: @cohere.com or @cw.cohere email alias" redacted as "from [redacted] [redacted] email
#: alias" on all 143 jobs of the board — a bare domain is not a contact, and eating the
#: word in front of it corrupts the ad body we sell.
_EMAIL = re.compile(
    r"""(?<![\w.+-])
        [A-Za-z0-9._%+-]+
        (?:@|\s?(?:\(at\)|\[at\])\s?)
        [A-Za-z0-9.-]+\.[A-Za-z]{2,24}
        (?![\w-])""",
    re.X,
)

#: The ``00`` international prefix carries a tighter lookbehind than ``+``: with the
#: shared ``(?<![\w.])`` it started inside a thousands group, so Crusoe's live line
#: "paid in the range of up to $215,000 - 260.000 + Bonus" matched from the ``00`` of
#: ``215,000`` through ``260.000`` and shipped "$215,[redacted] + Bonus" — the ad body
#: corrupted and, because redaction runs *before* §4.5.3 step 2, the pay range destroyed
#: with it. A ``+`` still redacts wherever it appears, which is where real numbers live.
#: The national trunk form is the *reason* `redactContacts` defaults to on. §5.6 and §5.8
#: name German- and Dutch-language SME ads as the target population, and those ads write
#: "Tel. 030 / 12 34 56 78", never "+49 30 …" — so the two international branches missed
#: every number the §15.5 Art. 6(1)(f) balance test rests on this regex catching (V1 H1).
#: The leading `0` plus the currency/comma lookbehind keeps it off salary and year ranges,
#: and `_MIN_PHONE_DIGITS` suppresses the short false positives that remain.
_PHONE = re.compile(
    r"""
      (?<![\w.])\+\d[\d\s().\-/]{6,}\d                   # international: +49 30 1234567
    | (?<![\w.,])00\d[\d\s().\-/]{6,}\d                  # international: 0049 30 1234567
    | (?<![\w.$€£₹¥,])0\d{1,4}[\s./\-]{1,3}\d[\d\s().\-/]{4,}\d(?![\d\w])  # 030 / 12 34 56 78
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


#: §15.2 / V1 B1 / V3 S4: `includeRawJson` used to attach the untouched provider payload,
#: so a run with `redactContacts: true` shipped the redacted ad body *and* the recruiter's
#: phone number in the same dataset row (`mailbox_email` is on every live Recruitee offer).
#: Contact-shaped keys go at every depth, whatever the provider calls them.
_RAW_DENY = re.compile(
    r"^(creator|activeJobApplication|recruiter|hiringManager|hiring_manager|contact|owner)$"
    r"|e?mail|phone|tel",
    re.IGNORECASE,
)

#: Ad bodies live inside `raw` too, and the denylist above does not touch them. Dropping
#: them is the honest option: the buyer already has the body through `includeDescription`,
#: where redaction is applied and `descriptionRedacted` says so.
_RAW_BODY_KEYS = frozenset(
    {
        "content",
        "description",
        "descriptionPlain",
        "descriptionHtml",
        "descriptionBody",
        "descriptionBodyPlain",
        "additional",
        "additionalPlain",
        "jobDescriptions",
        "requirements",
        "lists",
    }
)

#: Provider payloads are shallow; the bound is a cycle/blow-up guard, not a policy.
_RAW_MAX_DEPTH = 8


def strip_contact_fields(value: Any, *, redact: bool = True, depth: int = 0) -> Any:
    """Drop contact-shaped and body keys from a provider payload, at every depth.

    ``redact`` additionally runs :func:`redact_text` over the remaining string leaves, so
    an address written into an unrelated field (a `notes` blob, a custom question) cannot
    ride out under a key the denylist never heard of.
    """
    if depth > _RAW_MAX_DEPTH:
        return None
    if isinstance(value, dict):
        return {
            key: strip_contact_fields(item, redact=redact, depth=depth + 1)
            for key, item in value.items()
            if not _RAW_DENY.search(str(key)) and str(key) not in _RAW_BODY_KEYS
        }
    if isinstance(value, list):
        return [strip_contact_fields(item, redact=redact, depth=depth + 1) for item in value]
    if redact and isinstance(value, str):
        return redact_text(value)[0]
    return value
