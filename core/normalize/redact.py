"""Contact redaction (SPEC v2 §4.5.3, §15.5) — emails, phones, contact channels and
label-anchored contact names out.

``redactContacts`` defaults to **on** (§4.1) because German- and Dutch-language ads on
Personio and Recruitee conventionally close with a named contact. It is the condition on
which ``includeDescription`` was flipped to default **on**: a default run now carries the
ad body, so the redactor has to reach further than a mailbox.

**What this does not do:** it is label-anchored, not a name recogniser. A person named in
running prose — "you will report directly to Anna Schmidt" — has no pattern to match and
survives. That is deliberate: guessing at names in free text mangles the ad body we sell.
`PRIVACY.md` and the README say so in those words, and the Store listing must not claim
otherwise. Role mailboxes (`jobs@`, `careers@`) are removed too — they are not personal
data, but the email pattern cannot tell them apart and buyers have the apply URL anyway.

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
#: and `_MIN_PHONE_DIGITS` suppresses the short false positives that remain. The en/em
#: dash is in the separator class because live Dutch Recruitee ads write "bel/app met
#: onze recruiter … via 06–29140410" — an ASCII-only class shipped that number whole.
_PHONE = re.compile(
    r"""
      (?<![\w.])\+\d[\d\s().\-/]{6,}\d                   # international: +49 30 1234567
    | (?<![\w.,])00\d[\d\s().\-/]{6,}\d                  # international: 0049 30 1234567
    | (?<![\w.$€£₹¥,])0\d{1,4}[\s./\-–—]{1,3}\d[\d\s().\-/–—]{4,}\d(?![\d\w])  # 030 / 12 34 56 78
    | (?<![\w.$€£₹¥,])\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?![\d\w])   # NANP: (555) 123-4567
    """,
    re.X,
)

#: A phone number has at least this many digits; the guard keeps the loose international
#: pattern off "+1 more" and off long product codes.
_MIN_PHONE_DIGITS = 7

#: Contact channels that are neither an email nor a phone number (L3 P1 class 2). Each is
#: an identifier *and* a route, so the reasoning that redacts a mailbox redacts these. The
#: scheme is optional because ads write them bare. Only personal profile paths are listed:
#: `linkedin.com/company/acme` is the employer, not a person, and must survive.
_CHANNEL = re.compile(
    r"""(?<![\w/])(?:https?://)?(?:[\w-]+\.)*
        (?: linkedin\.com/(?:in|pub)
          | xing\.com/(?:profile|xbp)
          | calendly\.com
          | wa\.me
          | t\.me | telegram\.me
        )
        /[\w%.@~+-]+ (?:/[\w%.@~+-]*)*
        (?:\?[\w%&=.,+-]*)?      # live: wa.me/49…?text=%23bewerbung — the tail goes too""",
    re.X | re.I,
)

#: `@handle` only where a messaging/social label introduces it. Unanchored, `@name` would
#: eat Cohere's live "coming from an @cohere.com … email alias" line, which the bare-domain
#: test locks down as prose.
_HANDLE = re.compile(
    r"(?P<label>\b(?:LinkedIn|Xing|WhatsApp|Telegram|Calendly|Skype|Signal)\b[\s:]{0,3})"
    r"(?P<name>@[\w.]{2,})",
    re.I,
)

#: Label-anchored contact names (V1 L11, L3 P1 class 1) — the German/Dutch/English SME
#: closing convention that the §15.5 Art. 6(1)(f) balance rests on. Deliberately narrow:
#:
#: * only where the ad *labels* the following token as a contact, so ordinary prose ("you
#:   will report directly to Anna Schmidt") is untouched — a name recogniser loose enough
#:   to catch that one shreds the ad body, which is the thing buyers pay for;
#: * the label must end in a colon (never a hyphen: `Contact-Center Manager` is a job, not
#:   a person), so `Contact Center Manager` never even opens a match;
#: * two capitalised tokens minimum **and** a closing-punctuation lookahead, which is what
#:   keeps it off German capitalised common nouns — in "Kontakt: Unser Recruiting Team
#:   freut sich auf Sie" every candidate span is followed by another word, so nothing
#:   matches at all.
#:
#: Only the `name` group is replaced: "Ihre Ansprechpartnerin: [redacted]" reads correctly
#: and tells the buyer what was taken.
_NAMED_CONTACT = re.compile(
    r"""(?P<label>(?i:
            (?: Ansprechpartner(?:in)?
              | Kontaktperson | Kontakt
              | Contactpersoon | Contactperson | Contact
              | Hiring\ manager | Recruiter | Recruiting\ contact
              | Your\ (?:contact|recruiter)
            )\s*:
          | Questions\?\s*(?:Contact|Reach\ out\ to)
          | Bei\ Fragen\ (?:wenden\ Sie\ sich\ an|steht\ Ihnen)
          | Neem\ contact\ op\ met
        ))
        (?P<sep>(?:\s|&nbsp;|<[^>]{1,40}>)*)
        (?P<name>
            (?:(?:Herr|Frau|Dhr\.|Mevr\.|Mr\.|Ms\.|Mrs\.|Dr\.)\s+)?
            [A-ZÀ-ÖØ-Þ][\w'’-]+
            # Dutch/German/French tussenvoegsel: "Anke de Vries", "Peter van der Berg"
            (?:\s+(?:(?:de|den|der|van|von|di|du|le|la|del|da|dos)\s+){0,2}
                [A-ZÀ-ÖØ-Þ][\w'’-]+){1,2}
        )
        (?=[ \t ]*(?:[,.;:!?)\]<\r\n]|$))""",
    re.X,
)


def _phone_sub(match: re.Match[str]) -> str:
    text = match.group(0)
    if sum(c.isdigit() for c in text) < _MIN_PHONE_DIGITS:
        return text
    return PLACEHOLDER


def _keep_label(match: re.Match[str]) -> str:
    """Replace only the ``name`` group, leaving the label that introduced it in place."""
    return match.group(0)[: match.start("name") - match.start(0)] + PLACEHOLDER


def redact_text(value: str | None) -> tuple[str | None, bool]:
    """``(text, something_was_removed)``.

    Channels first (a profile URL is unambiguous and must not be half-eaten by the phone
    pattern), then emails, phones, labelled handles and finally labelled contact names —
    which run last so an already-redacted address cannot be read as the name.

    Only the matched spans are replaced, so HTML markup around them survives intact —
    ``<a href="mailto:[redacted]">`` is still a well-formed tag.
    """
    if not value:
        return value, False
    redacted = _CHANNEL.sub(PLACEHOLDER, value)
    redacted = _EMAIL.sub(PLACEHOLDER, redacted)
    redacted = _PHONE.sub(_phone_sub, redacted)
    redacted = _HANDLE.sub(_keep_label, redacted)
    redacted = _NAMED_CONTACT.sub(_keep_label, redacted)
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
