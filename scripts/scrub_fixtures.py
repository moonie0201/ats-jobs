#!/usr/bin/env python3
"""Replace third-party ad prose in `tests/fixtures/` with synthetic filler.

**Why this exists.** The fixtures are real payloads from real boards, and a job
advertisement body is the *employer's* copyrighted literary work — not the ATS vendor's,
and not ours. This repository is public, so committing the raw bodies published ~1 MB of
eleven named employers' works to the world under the repository's MIT `LICENSE`, which we
have no right to grant. It also published two named individuals' work email addresses.

The tests never assert on the prose. They assert on *shape*: which key carries the body,
whether it arrives entity-escaped, how many `<h3>` sections there are, that a contact line
is redacted, that a pay range parses. So the fix is to keep the shape and throw the prose
away:

* every HTML tag, attribute, entity and CDATA wrapper is preserved byte-for-byte;
* every **word** in a text node becomes a filler word;
* every **number**, currency symbol and percentage is preserved verbatim (a salary figure
  is a fact, and the salary parser tests need it);
* an email address survives only if its local part is a role mailbox
  (`careers@`, `accommodations@`, …) — a role mailbox is a corporate contact point, not
  personal data, and the redaction tests need one to fire on. Anything else becomes
  `careers@<same-domain>`.

Run it after `refresh_fixtures.py` — that script calls it for you. It is idempotent.

    python scripts/scrub_fixtures.py            # rewrite in place
    python scripts/scrub_fixtures.py --check    # exit 1 if any fixture still has prose
    python scripts/scrub_fixtures.py --selftest # offline asserts, no files touched
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import zlib
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

#: Keys whose string values are advertisement prose, across all six providers. Matched by
#: key name at any depth, so `translations.nl.description` is caught with `description`.
BODY_KEYS = frozenset(
    {
        "additional",
        "additionalPlain",
        "blurb",
        "company",
        "content",
        "description",
        "descriptionBody",
        "descriptionBodyPlain",
        "descriptionHtml",
        "descriptionPlain",
        "legalNotice",
        "opening",
        "openingPlain",
        "requirements",
        "role",
        "salaryDescription",
        "salaryDescriptionPlain",
    }
)

#: Below this a value is a label, a title or a slug, not an advertisement body. Rippling's
#: `url` and Recruitee's `company_name` live under keys we would otherwise rewrite.
MIN_BODY_CHARS = 200

#: Deliberately bland, deliberately ours. 24 words so a long paragraph does not read as one
#: repeated token, short enough that nobody mistakes it for real copy.
_VOCAB = (
    "sample placeholder filler synthetic fixture text stands in for the original "
    "advertisement body which is not reproduced here see scripts scrub fixtures dot py "
    "for why this file carries structure only and no employer prose at all"
).split()

#: Already-filler tokens pass through untouched, which is what makes the pass idempotent —
#: without it a second run maps every filler word onto a different filler word and `--check`
#: could never be clean. The words are all common function words, so nothing expressive
#: survives on the back of this.
_VOCAB_SET = frozenset(_VOCAB)

#: Kept verbatim inside a text node: figures, money, percentages, ranges. Facts, not prose,
#: and `core.normalize.salary` is tested against them.
_KEEP = re.compile(r"^[\$€£₹¥]?[\d][\d.,]*(?:[kK]|%|\+)?$|^[-–—]$|^[\$€£₹¥]$")

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")

#: A mailbox a company publishes as a contact point for a role or a function. Not a natural
#: person, so it is not personal data and it stays — the redaction tests fire on it.
_ROLE_LOCALPARTS = re.compile(
    r"^(job|jobs|career|careers|recruit\w*|talent|hiring|hr|people|apply|"
    r"\w*accommodation\w*|accessibility|contact|info|hello|support|privacy|legal|press)"
    r"([.\-_+]|$)",
    re.IGNORECASE,
)


def _fill_words(text: str) -> str:
    """Same token count, same numbers, no prose. Whitespace inside the node is preserved.

    The filler word is chosen by hashing the *word*, not by counting position, so the same
    source word always yields the same filler. Lever concatenates `opening` +
    `descriptionBody` into `description` and ships all three; a positional seed scrubbed
    each of them differently and broke the containment the payload guarantees.
    """
    out: list[str] = []
    for token in re.split(r"(\s+)", text):
        # `@` passes through so :func:`_scrub_email` gets a whole address to judge, rather
        # than a filler word where the local part used to be.
        if (
            not token
            or token.isspace()
            or "@" in token
            or token in _VOCAB_SET
            or _KEEP.match(token)
        ):
            out.append(token)
            continue
        out.append(_VOCAB[zlib.crc32(token.encode()) % len(_VOCAB)])
    return "".join(out)


def _scrub_email(match: re.Match[str]) -> str:
    address = match.group(0)
    local, _, domain = address.partition("@")
    return address if _ROLE_LOCALPARTS.match(local) else f"careers@{domain}"


class _Skeleton(HTMLParser):
    """Emit the input's markup unchanged and its text nodes as filler."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []

    def _raw(self) -> str:
        return self.get_starttag_text() or ""

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        self.out.append(self._raw())

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        self.out.append(self._raw())

    def handle_endtag(self, tag: str) -> None:
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not data.strip():
            self.out.append(data)
            return
        self.out.append(_fill_words(data))

    def handle_entityref(self, name: str) -> None:
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.out.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.out.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.out.append(f"<!{decl}>")


def scrub_body(text: str) -> str:
    """Structure in, structure out — with the words replaced.

    Handles the three shapes the six providers ship: plain text, inline HTML, and HTML that
    arrived entity-escaped (Greenhouse's ``content``). The escaped case is unescaped,
    rewritten and re-escaped, so the "unescape exactly once" contract still holds.
    """
    escaped = "&lt;" in text and "<" not in text
    source = html.unescape(text) if escaped else text
    parser = _Skeleton()
    parser.feed(source)
    parser.close()
    result = "".join(parser.out)
    result = _EMAIL.sub(_scrub_email, result)
    return html.escape(result) if escaped else result


def scrub_json(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: scrub_json(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_json(v, key) for v in value]
    if isinstance(value, str):
        if key in BODY_KEYS and len(value) >= MIN_BODY_CHARS:
            return scrub_body(value)
        return _EMAIL.sub(_scrub_email, value)
    return value


#: Personio ships XML, and its bodies live in CDATA under ``<jobDescription><value>``.
_CDATA = re.compile(r"(<!\[CDATA\[)(.*?)(\]\]>)", re.S)


def scrub_xml(text: str) -> str:
    scrubbed = _CDATA.sub(lambda m: m.group(1) + scrub_body(m.group(2)) + m.group(3), text)
    return _EMAIL.sub(_scrub_email, scrubbed)


def scrub_file(path: Path) -> bool:
    """Rewrite one fixture. Returns True when the bytes changed."""
    original = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        try:
            loaded = json.loads(original)
        except ValueError:
            return False  # malformed-payload fixtures are shape tests; leave them alone
        updated = json.dumps(scrub_json(loaded), ensure_ascii=False, indent=1) + "\n"
    else:
        updated = scrub_xml(original)
    if updated == original:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def selftest() -> int:
    plain = scrub_body("<p>Build the future with us</p>")
    assert plain.startswith("<p>") and plain.endswith("</p>")
    assert len(plain.split()) == 5 and "future" not in plain, plain

    # Deterministic on content, not position: the same source text scrubs the same way
    # wherever it appears, which is what keeps Lever's opening/body/description containment.
    assert scrub_body("<p>a b c</p>") in scrub_body("<p>x</p><p>a b c</p>")

    # Markup, attributes and entity escaping survive; the Greenhouse round-trip holds.
    escaped = "&lt;div class=&quot;x&quot;&gt;&lt;h2&gt;About Us&lt;/h2&gt;&lt;/div&gt;"
    out = scrub_body(escaped)
    assert "&lt;div class=&quot;x&quot;&gt;&lt;h2&gt;" in out, out
    assert "About" not in out and "Us" not in out, out

    # Numbers and currency stay, so the salary parser still has something to parse.
    money = scrub_body("<p>We pay $150,000 - $200,000 per year</p>")
    assert "$150,000" in money and "$200,000" in money, money

    # Role mailbox survives; a named individual's does not.
    assert "accommodations@palantir.com" in scrub_body(
        "<p>Write to accommodations@palantir.com</p>"
    )
    named = scrub_json({"description": "x" * 300 + " jan.dijkstra@acme.nl"})["description"]
    assert named.endswith("careers@acme.nl"), named

    # Idempotent: a second pass is a no-op on the words that are already filler.
    once = scrub_body("<p>Build the future with us</p>")
    assert scrub_body(once) == once, once

    # Short values under a body key are labels, not prose.
    assert scrub_json({"company": "Airbnb"})["company"] == "Airbnb"

    # CDATA wrappers are preserved; the prose inside is not.
    xml = "<value><![CDATA[<strong>Join our mission</strong>]]></value>"
    assert scrub_xml(xml).startswith("<value><![CDATA[<strong>")
    assert "mission" not in scrub_xml(xml)

    print("selfcheck ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if anything would change")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()

    paths = sorted(p for p in FIXTURES.rglob("*") if p.suffix in {".json", ".xml"})
    changed = [p for p in paths if scrub_file(p)] if not args.check else []
    if args.check:
        # Dry run: scrub into memory and compare.
        for path in paths:
            before = path.read_text(encoding="utf-8")
            if scrub_file(path):
                path.write_text(before, encoding="utf-8")
                changed.append(path)
        if changed:
            print("unscrubbed fixture prose in:", *(str(p) for p in changed), sep="\n  ")
            return 1
        print(f"{len(paths)} fixtures clean")
        return 0

    for path in changed:
        print(f"scrubbed {path.relative_to(ROOT)}")
    print(f"{len(changed)}/{len(paths)} fixtures rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
