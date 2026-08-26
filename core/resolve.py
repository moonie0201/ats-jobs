"""Turn one `companies` entry into a :class:`~core.models.Ref` (SPEC v2 §5.11).

Order of interpretation: explicit prefix, URL, bare token, domain. A bare token that the
directory cannot resolve is an error row, never a probe — §5.11 rule 5 exists because
probing 15 providers per unknown slug is what makes a competitor's runs slow.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from core.models import PROVIDERS, Ref

#: `workday:` is accepted here even though it is not in `PROVIDERS`: A7 ships the adapter
#: and §5.11's own example is `workday:nvidia/NVIDIAExternalCareerSite`. Whether an
#: adapter exists for a resolved provider is the registry's call, not the parser's.
KNOWN_PREFIXES: tuple[str, ...] = (*PROVIDERS, "workday")

#: §5.11: every class admits uppercase and is anchored with a lookahead, so a mixed-case
#: slug either matches whole or fails cleanly. It must never capture a fragment.
#: Each pattern is anchored to the start of the entry or to a ``//`` authority, so a
#: career-site host can only be read out of the *host* position: without it
#: ``https://attacker.example/?x=jobs.lever.co/palantir`` resolved to Lever `palantir`
#: (V3 S12).
HOST_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?:\A|//)(?:job-boards|boards)\.greenhouse\.io/"
            r"(?:embed/job_board\?for=)?([A-Za-z0-9_.-]+)(?=[/?#]|$)"
        ),
        "greenhouse",
    ),
    (re.compile(r"(?:\A|//)jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)(?=[/?#]|$)"), "lever"),
    (re.compile(r"(?:\A|//)jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)(?=[/?#]|$)"), "ashby"),
    (re.compile(r"(?:\A|//)([A-Za-z0-9-]+)\.recruitee\.com(?=[/?#]|$)"), "recruitee"),
    (re.compile(r"(?:\A|//)ats\.rippling\.com/([A-Za-z0-9_.-]+)(?=[/?#]|$)"), "rippling"),
    (re.compile(r"(?:\A|//)([A-Za-z0-9-]+)\.jobs\.personio\.(?:de|com)(?=[/?#]|$)"), "personio"),
)

RESERVED_SLUG = re.compile(r"^(embed|api|www|sitemap|robots|assets|static)$", re.IGNORECASE)
#: A board slug is one DNS label or one path segment. The old filter was *negative* — it
#: rejected ``%`` and eight reserved words and passed everything else — so `?`, `#`, `:`
#: and `@` survived into ``https://{slug}.recruitee.com/...``, where they terminate the
#: URL authority: ``recruitee:localhost:6379?`` reached localhost:6379 (V3 S1).
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(?!//)(\S.*)$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
URLISH_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://|^//|/")


@dataclass(slots=True)
class Unresolved:
    """A `companies` entry that could not become a Ref — becomes one free error row."""

    input: str
    status: str
    message: str
    candidates: list[str] = field(default_factory=list)


class DirectoryLookup(Protocol):
    """The slice of :class:`core.directory.Directory` this module needs."""

    def lookup(self, token: str, providers: Sequence[str] | None = None) -> list[Ref]: ...

    def lookup_domain(self, domain: str, providers: Sequence[str] | None = None) -> list[Ref]: ...


def valid_slug(slug: str) -> bool:
    """Positive charset only (V3 S1). Every ``Ref`` in the codebase is born through here
    or through :func:`core.directory.Directory._to_refs`, which calls it too."""
    return bool(SLUG_RE.match(slug)) and not RESERVED_SLUG.match(slug)


def parse_prefix(entry: str) -> Ref | None:
    """`lever:palantir`, `workday:nvidia/NVIDIAExternalCareerSite` (§5.11 rule 1)."""
    match = PREFIX_RE.match(entry.strip())
    if not match:
        return None
    provider = match.group(1).lower()
    if provider not in KNOWN_PREFIXES:
        return None
    rest = match.group(2).strip().strip("/")
    slug, _, site = rest.partition("/")
    if not valid_slug(slug):
        return None
    # `site` is the second path segment of a `workday:nvidia/NVIDIAExternalCareerSite`
    # style entry and reaches a URL too, so it gets the same charset (V3 S1/S11).
    if site and not SLUG_RE.match(site):
        return None
    return Ref(provider=provider, slug=slug, site=site or None, input=entry)


def parse_url(entry: str) -> Ref | None:
    """Match the §5.11 host table. Returns None for anything not on it."""
    text = entry.strip()
    for pattern, provider in HOST_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        slug = match.group(1)
        if not valid_slug(slug):
            return None
        region = "eu" if provider == "lever" and ".eu.lever.co" in match.group(0) else None
        return Ref(provider=provider, slug=slug, region=region, input=entry)
    return None


def looks_like_url(entry: str) -> bool:
    return bool(URLISH_RE.search(entry.strip()))


def host_of(entry: str) -> str:
    """Bare host of a URL-ish entry, `www.` stripped."""
    text = entry.strip()
    if "://" not in text:
        text = "//" + text
    host = (urlsplit(text).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def needs_directory(entry: str) -> bool:
    """True when resolving this entry requires the directory (§6.6 lazy-load hook)."""
    return parse_prefix(entry) is None and parse_url(entry) is None


def resolve(
    entry: str,
    *,
    providers: Sequence[str] | None = None,
    directory: DirectoryLookup | None = None,
) -> Ref | Unresolved:
    """One entry -> Ref, or an Unresolved carrying the §5.12 status for its error row.

    ``providers`` restricts directory lookups only; an explicit prefix or URL always wins
    (§4.1, §5.11).
    """
    raw = entry.strip()
    if not raw:
        return Unresolved(entry, "not_found", "empty entry")

    ref = parse_prefix(raw) or parse_url(raw)
    if ref is not None:
        return ref

    urlish = looks_like_url(raw)
    token = host_of(raw) if urlish else raw
    if not token:
        return Unresolved(entry, "not_found", f"unrecognised career-site URL: {entry}")

    is_domain = bool(DOMAIN_RE.match(token))
    if directory is None:
        return Unresolved(
            entry,
            "unresolved_domain" if is_domain else "not_found",
            "company directory unavailable; add a career-site URL or an ATS prefix",
        )

    hits = (
        directory.lookup_domain(token, providers)
        if is_domain
        else directory.lookup(token, providers)
    )
    if len(hits) == 1:
        hit = hits[0]
        return Ref(
            provider=hit.provider,
            slug=hit.slug,
            site=hit.site,
            region=hit.region,
            domain=hit.domain,
            input=entry,
        )
    if not hits:
        if is_domain:
            return Unresolved(
                entry,
                "unresolved_domain",
                "add a career-site URL or an ATS prefix",
            )
        return Unresolved(
            entry,
            "not_found",
            f"no directory match for {entry!r}; add a career-site URL or an ATS prefix",
        )

    candidates = [f"{hit.provider}:{hit.slug}" for hit in hits]
    return Unresolved(
        entry,
        "unconfirmed",
        f"{entry!r} matches several companies; pick one: {', '.join(candidates)}",
        candidates,
    )
