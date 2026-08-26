#!/usr/bin/env python3
"""Build the public ATS company directory (SPEC v2 §6.2 sources, §6.3 validation, §6.4 format).

Discovery -> validation -> two files:

  core/data/companies.seed.jsonl.gz   the full §6.4 record, baked into the Docker image
  ../ats-directory/companies.jsonl    the §6.6 public projection, committed to the public repo
  ../ats-directory/companies.jsonl.gz the same rows gzipped, which is what §6.6's read path
                                      actually fetches from jsDelivr / raw.githubusercontent

**Common Crawl is not a source.** §6.2 removed it outright (V1 L2): `index.commoncrawl.org`
serves `Disallow: /` with an Allow list that does not include `/{CRAWL}-index`, and
`data.commoncrawl.org` disallows everything. The CDX index queries this script would
otherwise have issued are exactly the requests that robots.txt refuses, and the CC ToU
grant is *non-sublicensable*, which is incompatible with re-publishing derived rows under
CC0. Discovery therefore runs on the sources §6.2 kept: Wayback CDX, SimplifyJobs, and the
HN "Who is hiring" thread via the documented Algolia API.

Two HTTP clients, deliberately:

* **Validation** uses :mod:`core.http`, so the probes inherit the Actor's User-Agent, the
  §5.12 retry table and the per-host token bucket — and inherit its host allowlist, which
  is the point. Nothing here can widen what the shipped Actor is allowed to reach.
* **Discovery** uses a plain client, because `web.archive.org` and `hn.algolia.com` are not
  on that allowlist and must never be added to it: the Actor has no business calling them.
  It carries its own 1 rps-per-host bucket and a small semaphore so it stays polite anyway.

Usage::

    python scripts/build_directory.py --cap 400
    python scripts/build_directory.py --selfcheck      # offline asserts, no network
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import html
import io
import json
import logging
import random
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from itertools import zip_longest
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.http import (  # noqa: E402
    DEFAULT_HEADERS,
    FetchError,
    NotFound,
    TokenBucket,
    make_client,
)
from core.resolve import parse_url, valid_slug  # noqa: E402

logger = logging.getLogger("build_directory")

PROVIDERS: tuple[str, ...] = (
    "greenhouse",
    "lever",
    "ashby",
    "recruitee",
    "rippling",
    "personio",
)

SEED_PATH = REPO / "core" / "data" / "companies.seed.jsonl.gz"
PUBLIC_PATH = REPO.parent / "ats-directory" / "companies.jsonl"

#: §6.6: the public repo publishes only these, "every field of which is the output of our
#: own live validation probe". `job_count`, `first_seen`, `checked_at` and `dead_since`
#: stay in the baked seed and the private history store (§7).
PUBLIC_FIELDS: tuple[str, ...] = ("provider", "slug", "site", "region", "name", "domain", "status")

# --------------------------------------------------------------------------------------
# §6.2 discovery
# --------------------------------------------------------------------------------------

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
HN_SEARCH = "https://hn.algolia.com/api/v1/search"

#: Candidate slugs only. §6.2: SimplifyJobs' `company_name` / `company_url` are "used only
#: to prioritise validation, never republished", so this script reads the `url` field and
#: nothing else — the name that reaches a published row comes from the provider's own API.
SIMPLIFY_URLS: tuple[str, ...] = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships"
    "/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions"
    "/dev/.github/scripts/listings.json",
)

#: Candidate rank: lower wins the cap. A slug carried by a live job feed today is a far
#: better use of a validation probe than an arbitrary decade-old Wayback capture, which is
#: precisely the prioritisation role §6.2 assigns these two sources.
RANK_SIMPLIFY, RANK_HN, RANK_WAYBACK = 0, 1, 2


@dataclass(frozen=True, slots=True)
class Candidate:
    provider: str
    slug: str
    region: str | None = None
    rank: int = RANK_WAYBACK

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider, self.slug.casefold())


@dataclass(frozen=True, slots=True)
class WaybackQuery:
    """One CDX query. ``url`` may carry ``{p}``, in which case it is sliced by prefix.

    Slicing exists because the CDX server truncates a long scan: an unsliced
    `boards.greenhouse.io/*` root-URL query returns partial results after ~90 s and stops
    around the letter *d*, so a single query yields an alphabetically biased directory.
    Per-prefix queries each return complete.
    """

    url: str
    filter: str
    match_type: str | None = None
    region: str | None = None


def _root_filter(host: str) -> str:
    """Root URLs only: `https://{host}/{slug}`, no deeper path.

    §6.2's `filter=original:{root-regex}` is what keeps the result set to roughly one row
    per company instead of one row per archived job ad.
    """
    return rf"^https?://{re.escape(host)}/[A-Za-z0-9][A-Za-z0-9_.-]{{1,59}}/?$"


def _sub_filter(host: str, path: str = "") -> str:
    """Subdomain-carried slugs (Recruitee, Personio)."""
    return rf"^https?://[A-Za-z0-9][A-Za-z0-9-]*\.{re.escape(host)}/{path}.*$"


WAYBACK_QUERIES: dict[str, tuple[WaybackQuery, ...]] = {
    "greenhouse": (
        WaybackQuery("boards.greenhouse.io/{p}*", _root_filter("boards.greenhouse.io")),
        WaybackQuery("job-boards.greenhouse.io/{p}*", _root_filter("job-boards.greenhouse.io")),
    ),
    "lever": (
        WaybackQuery("jobs.lever.co/{p}*", _root_filter("jobs.lever.co")),
        WaybackQuery("jobs.eu.lever.co/{p}*", _root_filter("jobs.eu.lever.co"), region="eu"),
    ),
    "ashby": (WaybackQuery("jobs.ashbyhq.com/{p}*", _root_filter("jobs.ashbyhq.com")),),
    "rippling": (WaybackQuery("ats.rippling.com/{p}*", _root_filter("ats.rippling.com")),),
    # `matchType=domain` cannot be sliced by subdomain prefix, but it does not need to be:
    # these two answer a 40k-row query in ~4 s because one capture per company is the norm.
    "recruitee": (
        WaybackQuery("recruitee.com", _sub_filter("recruitee.com", "o/"), match_type="domain"),
    ),
    "personio": (
        WaybackQuery("jobs.personio.de", _sub_filter("jobs.personio.de"), match_type="domain"),
        WaybackQuery("jobs.personio.com", _sub_filter("jobs.personio.com"), match_type="domain"),
    ),
}

#: Spread across the keyspace rather than walking `a..z`, so a run that stops early once a
#: provider has enough candidates still holds companies from the whole alphabet.
DEFAULT_PREFIXES = ("a", "m", "e", "s", "i", "c", "p", "g", "t", "b", "l", "r", "1")


class PoliteClient:
    """Discovery transport: 1 rps per host, small global concurrency, honest UA."""

    def __init__(self, http: httpx.AsyncClient, *, rate: float = 1.0, concurrency: int = 3):
        self._http = http
        self._rate = rate
        self._sem = asyncio.Semaphore(concurrency)
        self._buckets: dict[str, TokenBucket] = {}

    async def get(self, url: str, **kwargs: Any) -> httpx.Response | None:
        host = (httpx.URL(url).host or "").lower()
        bucket = self._buckets.setdefault(host, TokenBucket(self._rate))
        async with self._sem:
            await bucket.acquire()
            try:
                response = await self._http.get(url, **kwargs)
            except httpx.HTTPError as exc:
                logger.warning("discovery GET failed %s: %s", url, type(exc).__name__)
                return None
        if response.status_code >= 400:
            logger.warning("discovery GET %s -> %s", url, response.status_code)
            return None
        return response


def candidates_from_urls(urls: Any, rank: int) -> list[Candidate]:
    """Any iterable of URL-ish strings -> candidates, via the Actor's own §5.11 parser.

    Reusing :func:`core.resolve.parse_url` rather than a second set of host regexes is what
    keeps discovery and the shipped Actor agreeing on what a slug is — including the V3 S1
    charset gate and the `jobs.eu.lever.co` -> ``region="eu"`` rule.
    """
    out: list[Candidate] = []
    for url in urls:
        if not isinstance(url, str):
            continue
        ref = parse_url(url)
        if ref is None or ref.provider not in PROVIDERS:
            continue
        out.append(Candidate(ref.provider, ref.slug, ref.region, rank))
    return out


async def from_simplify(client: PoliteClient) -> list[Candidate]:
    """§6.2 daily source. Two `listings.json` files; only the `url` field is read."""
    found: list[Candidate] = []
    for url in SIMPLIFY_URLS:
        response = await client.get(url, follow_redirects=True)
        if response is None:
            continue
        try:
            rows = response.json()
        except ValueError:
            logger.warning("simplify: unparseable body at %s", url)
            continue
        if not isinstance(rows, list):
            continue
        found += candidates_from_urls(
            (row.get("url") for row in rows if isinstance(row, dict)), RANK_SIMPLIFY
        )
    logger.info("simplify: %d candidates", len(found))
    return found


async def from_hn(client: PoliteClient, *, threads: int = 3) -> list[Candidate]:
    """§6.2 monthly source: the newest "Who is hiring" stories -> their comments -> URLs."""
    response = await client.get(
        HN_SEARCH, params={"tags": "story,author_whoishiring", "hitsPerPage": 40}
    )
    if response is None:
        return []
    try:
        hits = response.json().get("hits", [])
    except ValueError:
        return []
    # Algolia's relevance sort is not chronological; `created_at_i` is.
    stories = sorted(
        (
            h
            for h in hits
            if isinstance(h, dict) and "who is hiring" in (h.get("title") or "").lower()
        ),
        key=lambda h: h.get("created_at_i") or 0,
        reverse=True,
    )[:threads]

    found: list[Candidate] = []
    for story in stories:
        page = await client.get(
            HN_SEARCH,
            params={"tags": f"comment,story_{story['objectID']}", "hitsPerPage": 1000},
        )
        if page is None:
            continue
        try:
            comments = page.json().get("hits", [])
        except ValueError:
            continue
        urls: list[str] = []
        for comment in comments:
            raw = (comment.get("comment_text") or "") if isinstance(comment, dict) else ""
            # HN serves comment bodies HTML-escaped, `/` included as `&#x2F;`.
            text = html.unescape(raw)
            urls += [u.rstrip(".,;:)'\"") for u in re.findall(r'https?://[^\s"<>]+', text)]
        found += candidates_from_urls(urls, RANK_HN)
    logger.info("hn: %d candidates from %d threads", len(found), len(stories))
    return found


async def from_wayback(
    client: PoliteClient,
    provider: str,
    *,
    need: int,
    prefixes: tuple[str, ...],
    row_limit: int,
) -> list[Candidate]:
    """§6.2 monthly source. Stops as soon as ``need`` distinct slugs are in hand."""
    found: dict[tuple[str, str], Candidate] = {}
    for query in WAYBACK_QUERIES.get(provider, ()):
        slices = [query.url.format(p=p) for p in prefixes] if "{p}" in query.url else [query.url]
        for target in slices:
            if len(found) >= need:
                break
            params: dict[str, str] = {
                "url": target,
                "fl": "original",
                "collapse": "urlkey",
                "limit": str(row_limit),
                "filter": "original:" + query.filter,
            }
            if query.match_type:
                params["matchType"] = query.match_type
            response = await client.get(WAYBACK_CDX, params=params)
            if response is None:
                continue
            for cand in candidates_from_urls(response.text.splitlines(), RANK_WAYBACK):
                if cand.provider != provider:
                    continue
                if query.region and cand.region is None:
                    cand = Candidate(cand.provider, cand.slug, query.region, RANK_WAYBACK)
                found.setdefault(cand.key, cand)
        if len(found) >= need:
            break
    logger.info("wayback %s: %d candidates", provider, len(found))
    return list(found.values())


def merge_candidates(batches: list[list[Candidate]], cap: int) -> dict[str, list[Candidate]]:
    """Dedupe on `(provider, casefold(slug))`, keep the best-ranked spelling, apply the cap.

    §6.4 keeps a slug **verbatim as it validated** and indexes on the casefolded form,
    because Lever is case-sensitive (`Palantir` 404s where `palantir` works). Two spellings
    of one slug are therefore one candidate, and the one from the higher-quality source
    wins — a live feed's spelling is likelier to be the spelling that validates.
    """
    best: dict[tuple[str, str], Candidate] = {}
    for batch in batches:
        for cand in batch:
            current = best.get(cand.key)
            if current is None:
                best[cand.key] = cand
            elif cand.rank < current.rank:
                # Better source wins the spelling, but a region hint is never thrown away.
                best[cand.key] = Candidate(
                    cand.provider, cand.slug, cand.region or current.region, cand.rank
                )
            elif cand.region and not current.region:
                best[cand.key] = Candidate(
                    current.provider, current.slug, cand.region, current.rank
                )
    by_provider: dict[str, list[Candidate]] = defaultdict(list)
    for cand in best.values():
        by_provider[cand.provider].append(cand)
    for provider, cands in by_provider.items():
        # Rank first, then a stable spread inside a rank: Wayback comes back in SURT order,
        # so slicing it raw would cap the directory at everything starting with "a".
        cands.sort(key=lambda c: (c.rank, c.slug.casefold()))
        head = [c for c in cands if c.rank < RANK_WAYBACK]
        tail = [c for c in cands if c.rank == RANK_WAYBACK]
        random.Random(provider).shuffle(tail)
        by_provider[provider] = (head + tail)[:cap]
    return dict(by_provider)


# --------------------------------------------------------------------------------------
# §6.3 validation
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Probe:
    """One validation result. ``status`` is §6.3's `ok` / `dead` / `unconfirmed`."""

    provider: str
    slug: str
    status: str
    job_count: int | None = None
    name: str | None = None
    region: str | None = None
    site: str | None = None
    note: str = ""


def _first_str(values: Any) -> str | None:
    for value in values or ():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def _greenhouse(client: Any, cand: Candidate) -> Probe:
    slug = cand.slug
    # Board first, then jobs: the board body is ~40 bytes and 404s for a dead slug, so a
    # dead candidate costs one small request instead of one large one. §6.4's rule that
    # `name` comes from the provider's own response is what makes this call non-optional.
    board = await client.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
    name = board.get("name") if isinstance(board, dict) else None
    body = await client.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    jobs = body.get("jobs") or [] if isinstance(body, dict) else []
    total = (body.get("meta") or {}).get("total") if isinstance(body, dict) else None
    count = total if isinstance(total, int) else len(jobs)
    return Probe("greenhouse", slug, "ok", count, name)


async def _lever(client: Any, cand: Candidate) -> Probe:
    """§6.3, corrected row (V2 T-H6): 200-with-empty on **both** hosts is `unconfirmed`.

    v1 accepted "200 (even `[]`)" as ok, which recorded `region: null` for every EU tenant
    and cached the wrong host permanently.
    """
    hosts = [("api.lever.co", None), ("api.eu.lever.co", "eu")]
    if cand.region == "eu":
        hosts.reverse()
    seen_empty = False
    for host, region in hosts:
        try:
            # No `limit=1`: §6.3 wants a `job_count` on the row, and Lever's list response
            # is complete in one body (§5.2), so the count is free with the status.
            body = await client.get_json(f"https://{host}/v0/postings/{cand.slug}?mode=json")
        except NotFound:
            continue
        if isinstance(body, list) and body:
            return Probe("lever", cand.slug, "ok", len(body), None, region)
        seen_empty = True
    if seen_empty:
        return Probe("lever", cand.slug, "unconfirmed", 0, note="200_empty_both_hosts")
    raise NotFound(f"lever: 404 on both hosts: {cand.slug}")


async def _ashby(client: Any, cand: Candidate) -> Probe:
    body = await client.get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{cand.slug}?includeCompensation=true"
    )
    jobs = body.get("jobs") or [] if isinstance(body, dict) else []
    # §5.3's defensive filter, applied here too so the count matches what the Actor emits.
    listed = [j for j in jobs if not (isinstance(j, dict) and j.get("isListed") is False)]
    return Probe("ashby", cand.slug, "ok", len(listed))


async def _recruitee(client: Any, cand: Candidate) -> Probe:
    body = await client.get_json(f"https://{cand.slug}.recruitee.com/api/offers/")
    offers = body.get("offers") or [] if isinstance(body, dict) else []
    name = _first_str(o.get("company_name") for o in offers if isinstance(o, dict))
    return Probe("recruitee", cand.slug, "ok", len(offers), name)


async def _rippling(client: Any, cand: Candidate) -> Probe:
    body = await client.get_json(
        f"https://api.rippling.com/platform/api/ats/v1/board/{cand.slug}/jobs"
    )
    rows = body if isinstance(body, list) else []
    # §5.7: the list endpoint returns one row per (job x location) — 725 rows for 374 jobs
    # on the board measured. Counting rows would publish a job_count ~2x the truth.
    uuids = {r.get("uuid") for r in rows if isinstance(r, dict) and r.get("uuid")}
    return Probe("rippling", cand.slug, "ok", len(uuids))


def _parse_workzag(response: httpx.Response) -> Any:
    """defusedxml, never stdlib `xml.etree` — the XML is third-party (§5.8)."""
    from defusedxml.ElementTree import fromstring

    root = fromstring(response.content)
    if root.tag != "workzag-jobs":
        raise NotFound(f"personio: not a workzag-jobs feed: {response.url}")
    return root


async def _personio(client: Any, cand: Candidate) -> Probe:
    for tld in ("de", "com"):
        try:
            root = await client.get_json(
                f"https://{cand.slug}.jobs.personio.{tld}/xml?language=en",
                parse=_parse_workzag,
            )
        except NotFound:
            continue
        positions = root.findall("position")
        name = _first_str(p.findtext("subcompany") for p in positions)
        return Probe("personio", cand.slug, "ok", len(positions), name)
    raise NotFound(f"personio: 307 on both hosts: {cand.slug}")


VALIDATORS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "recruitee": _recruitee,
    "rippling": _rippling,
    "personio": _personio,
}


async def validate(client: Any, cand: Candidate, sem: asyncio.Semaphore) -> Probe:
    """One candidate -> one §6.3 verdict. Never raises; a transport failure is
    `unconfirmed`, which is the honest answer — the slug may well be alive."""
    async with sem:
        try:
            probe = await VALIDATORS[cand.provider](client, cand)
        except NotFound:
            return Probe(cand.provider, cand.slug, "dead", note="not_found")
        except FetchError as exc:
            return Probe(cand.provider, cand.slug, "unconfirmed", note=exc.status)
        except Exception as exc:  # one bad row must not end a 40 min run
            logger.warning("%s:%s validate error: %r", cand.provider, cand.slug, exc)
            return Probe(cand.provider, cand.slug, "unconfirmed", note="error")
    if probe.region is None:
        probe.region = cand.region
    return probe


# --------------------------------------------------------------------------------------
# §6.4 records
# --------------------------------------------------------------------------------------

LEGAL_SUFFIXES = (
    "incorporated", "corporation", "limited", "holdings", "group", "inc", "llc", "ltd",
    "llp", "plc", "gmbh", "mbh", "ag", "kg", "bv", "nv", "sa", "srl", "spa", "oy", "ab",
    "as", "aps", "co", "corp", "company", "technologies", "labs",
)  # fmt: skip


def name_norm(name: str | None) -> str | None:
    """§6.4: lowercase, strip legal suffixes, strip punctuation. A display and dedupe aid,
    **never a key** — `Directory` indexes it but `(provider, slug)` is the identity."""
    if not name:
        return None
    words = re.sub(r"[^\w\s]+", " ", name.casefold(), flags=re.UNICODE).split()
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words) or None


def load_previous(*paths: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Prior rows keyed on `(provider, casefold(slug))`.

    §6.3: "Never delete history — a company that turns hiring off and on again must keep
    its `first_seen`." A rebuild that dropped `first_seen` would silently reset every
    company's age, which is the one field a rebuild cannot recover.
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        if not path.exists():
            continue
        raw = path.read_bytes()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("provider") and row.get("slug"):
                out.setdefault((str(row["provider"]), str(row["slug"]).casefold()), {}).update(row)
    return out


def build_rows(
    probes: list[Probe],
    previous: dict[tuple[str, str], dict[str, Any]],
    today: str,
) -> list[dict[str, Any]]:
    """§6.4 records, sorted by `(provider, casefold(slug))` for stable diffs."""
    rows: list[dict[str, Any]] = []
    for probe in probes:
        key = (probe.provider, probe.slug.casefold())
        prior = previous.get(key, {})
        dead = probe.status != "ok"
        name = probe.name or prior.get("name")
        rows.append(
            {
                "provider": probe.provider,
                "slug": probe.slug,
                "site": probe.site or prior.get("site"),
                "region": probe.region or prior.get("region"),
                "name": name,
                "name_norm": name_norm(name),
                # §6.4: `domain` may only come from the provider's own response, yc-oss,
                # Wikidata or Clearbit. None of the six APIs returns one and §6.2 marks
                # Clearbit's bulk terms unverified, so it stays null rather than being
                # guessed. It is nullable by design and never blocks ingestion.
                "domain": prior.get("domain"),
                "status": probe.status,
                "job_count": probe.job_count,
                "first_seen": prior.get("first_seen") or today,
                "checked_at": today,
                "dead_since": (prior.get("dead_since") or today) if dead else None,
            }
        )
    # Rows this run did not probe survive untouched: a capped run is a partial refresh,
    # not a statement that everything it skipped has vanished.
    probed = {(r["provider"], r["slug"].casefold()) for r in rows}
    rows += [dict(row) for key, row in previous.items() if key not in probed]
    rows.sort(key=lambda r: (r["provider"], r["slug"].casefold()))
    return rows


def to_jsonl(rows: list[dict[str, Any]], fields: tuple[str, ...] | None = None) -> bytes:
    keep = (lambda r: {k: r.get(k) for k in fields}) if fields else (lambda r: r)
    return "".join(json.dumps(keep(r), ensure_ascii=False) + "\n" for r in rows).encode("utf-8")


def gzip_bytes(blob: bytes) -> bytes:
    """mtime=0 so an unchanged directory produces an unchanged blob — a rebuild that
    reshuffles bytes every night defeats jsDelivr's commit-pinned year-long cache."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:
        fh.write(blob)
    return buf.getvalue()


def write_outputs(rows: list[dict[str, Any]], seed: Path, public: Path) -> None:
    seed.parent.mkdir(parents=True, exist_ok=True)
    public.parent.mkdir(parents=True, exist_ok=True)
    seed.write_bytes(gzip_bytes(to_jsonl(rows)))
    public_blob = to_jsonl(rows, PUBLIC_FIELDS)
    public.write_bytes(public_blob)
    public.with_name(public.name + ".gz").write_bytes(gzip_bytes(public_blob))


# --------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Stats:
    found: int = 0
    probed: int = 0
    ok: int = 0
    dead: int = 0
    unconfirmed: int = 0
    zero_jobs: int = 0
    notes: Counter[str] = field(default_factory=Counter)

    @property
    def degraded(self) -> bool:
        """§5.12's provider-degradation guard: >90% of live boards returning zero jobs is
        a broken provider, not an empty one. This is the signature Workable presents."""
        return self.ok >= 10 and self.zero_jobs > 0.9 * self.ok


def summarise(candidates: dict[str, list[Candidate]], probes: list[Probe]) -> dict[str, Stats]:
    stats = {p: Stats(found=len(candidates.get(p, []))) for p in PROVIDERS}
    for probe in probes:
        st = stats.setdefault(probe.provider, Stats())
        st.probed += 1
        setattr(st, probe.status, getattr(st, probe.status) + 1)
        if probe.status == "ok" and not probe.job_count:
            st.zero_jobs += 1
        if probe.note:
            st.notes[probe.note] += 1
    return stats


def report(stats: dict[str, Stats], rows: list[dict[str, Any]], elapsed: float) -> str:
    lines = [
        f"{'provider':<12}{'found':>7}{'probed':>8}{'ok':>7}{'dead':>7}{'unconf':>8}{'0 jobs':>8}",
        "-" * 57,
    ]
    for provider in PROVIDERS:
        st = stats.get(provider, Stats())
        flag = "  <-- DEGRADED (§5.12)" if st.degraded else ""
        lines.append(
            f"{provider:<12}{st.found:>7}{st.probed:>8}{st.ok:>7}"
            f"{st.dead:>7}{st.unconfirmed:>8}{st.zero_jobs:>8}{flag}"
        )
    total_ok = sum(1 for r in rows if r["status"] == "ok")
    lines += [
        "-" * 57,
        f"rows written: {len(rows)}  (status=ok: {total_ok})",
        f"§6.7 M1 target >=2,000 validated ok rows: {'MET' if total_ok >= 2000 else 'not met'}",
        f"elapsed: {elapsed / 60:.1f} min",
    ]
    for provider in PROVIDERS:
        notes = stats.get(provider, Stats()).notes
        if notes:
            lines.append(f"  {provider} notes: {dict(notes.most_common(5))}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------


async def discover(
    providers: tuple[str, ...],
    cap: int,
    *,
    prefixes: tuple[str, ...],
    row_limit: int,
    use_wayback: bool,
    timeout: float,
) -> dict[str, list[Candidate]]:
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=timeout) as http:
        client = PoliteClient(http)
        batches = list(await asyncio.gather(from_simplify(client), from_hn(client)))
        merged = merge_candidates(batches, cap)
        if use_wayback:
            # Only top up what the cheap sources did not already fill (§6.2 cadence: the
            # daily feed is free, the monthly CDX scan is the expensive one).
            short = [p for p in providers if len(merged.get(p, [])) < cap]
            if short:
                logger.info("wayback top-up for: %s", ", ".join(short))
                batches += await asyncio.gather(
                    *(
                        from_wayback(
                            client,
                            provider,
                            need=cap - len(merged.get(provider, [])),
                            prefixes=prefixes,
                            row_limit=row_limit,
                        )
                        for provider in short
                    )
                )
    merged = merge_candidates(batches, cap)
    return {p: merged.get(p, []) for p in providers}


async def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    providers = tuple(p for p in args.providers.split(",") if p.strip()) or PROVIDERS
    unknown = [p for p in providers if p not in PROVIDERS]
    if unknown:
        raise SystemExit(f"unknown providers: {unknown}")

    candidates = await discover(
        providers,
        args.cap,
        prefixes=tuple(args.prefixes),
        row_limit=args.wayback_rows,
        use_wayback=not args.no_wayback,
        timeout=args.timeout,
    )
    # Round-robin the providers rather than concatenating them. §5.12: Greenhouse, Lever,
    # Ashby and Rippling are each *one shared host* for every company, so their probes
    # serialise on one 1 rps token bucket apiece. Grouped by provider, the first `cap`
    # tasks are all Greenhouse, they fill the semaphore, and every other provider starves
    # behind a bucket it does not even share — turning six overlapping ~7 min queues into
    # one ~45 min chain. Interleaved, the four buckets drain concurrently.
    todo = [
        c
        for group in zip_longest(*(candidates.get(p, []) for p in providers))
        for c in group
        if c is not None
    ]
    logger.info(
        "validating %d candidates at %d concurrent, %.1f rps/host",
        len(todo),
        args.concurrency,
        args.rate,
    )

    # §6.3 politeness: 1 rps per host, 10 concurrent overall, same User-Agent as the Actor.
    # `rate_limits={}` drops the §5.12 per-host overrides so `default_rate` governs every
    # host uniformly — validation is a bulk sweep and takes the stricter of the two caps.
    sem = asyncio.Semaphore(args.concurrency)
    client = make_client(
        timeout_secs=args.timeout,
        max_connections=args.concurrency,
        # `{}` (not None) drops §5.12's per-host overrides so one uniform cap governs every
        # host: §6.3's validation sweep is the stricter of the two rules.
        rate_limits={},
    )
    # `make_client` has no `default_rate` parameter and `core/http.py` belongs to another
    # lane, so the cap is set on the instance rather than by forking the hardened
    # constructor (which carries the SSRF request hook). Buckets are built lazily on first
    # use, so this lands before any request.
    client._default_rate = args.rate
    async with client:
        probes = list(await asyncio.gather(*(validate(client, c, sem) for c in todo)))

    today = date.today().isoformat()
    previous = load_previous(args.out_public, args.out_seed)
    rows = build_rows(probes, previous, today)
    if not args.dry_run:
        write_outputs(rows, args.out_seed, args.out_public)

    stats = summarise(candidates, probes)
    print(report(stats, rows, time.monotonic() - started))
    if not args.dry_run:
        gz = args.out_public.with_name(args.out_public.name + ".gz")
        print(f"\nwrote {args.out_seed}\nwrote {args.out_public}\nwrote {gz}")
    return 1 if any(st.degraded for st in stats.values()) else 0


def selfcheck() -> int:
    """One offline check for the logic that is not a one-liner (§6.2 extraction, §6.4
    records). Deliberately asserts, not a test framework: it runs anywhere, including in
    the container, with no fixtures to keep in sync."""
    cands = candidates_from_urls(
        [
            "https://boards.greenhouse.io/anthropic",
            "https://boards.greenhouse.io/anthropic/jobs/4461450008",
            "https://job-boards.greenhouse.io/stripe",
            "https://jobs.lever.co/palantir",
            "https://jobs.eu.lever.co/bunq",
            "https://jobs.ashbyhq.com/openai/1234",
            "https://bunq.recruitee.com/o/engineer",
            "https://ats.rippling.com/rippling/jobs/abc",
            "https://personio-gmbh.jobs.personio.de/job/1676226",
            "https://boards.greenhouse.io/embed/job_board?for=figma",
            "https://www.recruitee.com/blog",  # `www` is reserved, not a slug
            "https://example.com/careers",  # not an ATS host
            "https://boards.greenhouse.io/%2A%5C",  # V3 S1 charset gate
            12345,  # not a string
        ],
        RANK_WAYBACK,
    )
    got = {(c.provider, c.slug, c.region) for c in cands}
    assert ("greenhouse", "anthropic", None) in got, got
    assert ("greenhouse", "stripe", None) in got, got
    assert ("greenhouse", "figma", None) in got, got
    assert ("lever", "palantir", None) in got, got
    assert ("lever", "bunq", "eu") in got, got
    assert ("ashby", "openai", None) in got, got
    assert ("recruitee", "bunq", None) in got, got
    assert ("rippling", "rippling", None) in got, got
    assert ("personio", "personio-gmbh", None) in got, got
    assert not any(c.slug.casefold() == "www" for c in cands), got
    assert not any(c.provider not in PROVIDERS for c in cands), got
    assert all(valid_slug(c.slug) for c in cands), got

    # Case-sensitivity: `Palantir` and `palantir` are one key, and the better-ranked
    # source supplies the spelling that gets probed (§6.4 T-H9).
    merged = merge_candidates(
        [
            [Candidate("lever", "PALANTIR", None, RANK_WAYBACK)],
            [Candidate("lever", "palantir", None, RANK_SIMPLIFY)],
        ],
        cap=10,
    )
    assert [c.slug for c in merged["lever"]] == ["palantir"], merged

    # A region hint survives a same-rank collision.
    merged = merge_candidates(
        [
            [Candidate("lever", "bunq", None, RANK_WAYBACK)],
            [Candidate("lever", "bunq", "eu", RANK_WAYBACK)],
        ],
        cap=10,
    )
    assert merged["lever"][0].region == "eu", merged

    capped = merge_candidates([[Candidate("ashby", f"c{i}") for i in range(50)]], cap=7)
    assert len(capped["ashby"]) == 7, capped

    assert name_norm("Anthropic, PBC") == "anthropic pbc"
    assert name_norm("Sennder GmbH") == "sennder"
    assert name_norm("1KOMMA5° Group GmbH") == "1komma5"
    assert name_norm("Inc") == "inc", "a name that is only a suffix must not vanish"
    assert name_norm(None) is None

    today, before = "2026-08-26", "2026-01-01"
    previous = {
        ("greenhouse", "anthropic"): {
            "provider": "greenhouse",
            "slug": "anthropic",
            "first_seen": before,
            "domain": "anthropic.com",
            "dead_since": None,
        },
        ("lever", "gone"): {
            "provider": "lever",
            "slug": "gone",
            "first_seen": before,
            "dead_since": before,
        },
        ("ashby", "untouched"): {"provider": "ashby", "slug": "untouched", "first_seen": before},
    }
    rows = build_rows(
        [
            Probe("greenhouse", "anthropic", "ok", 533, "Anthropic"),
            Probe("lever", "gone", "dead"),
            Probe("recruitee", "new", "ok", 4, "Bunq Group"),
        ],
        previous,
        today,
    )
    by_key = {(r["provider"], r["slug"]): r for r in rows}
    assert by_key[("greenhouse", "anthropic")]["first_seen"] == before, "first_seen must survive"
    assert by_key[("greenhouse", "anthropic")]["dead_since"] is None
    assert by_key[("greenhouse", "anthropic")]["domain"] == "anthropic.com", "domain carried over"
    assert by_key[("lever", "gone")]["dead_since"] == before, "dead_since keeps the FIRST failure"
    assert by_key[("recruitee", "new")]["first_seen"] == today
    assert by_key[("recruitee", "new")]["name_norm"] == "bunq"
    assert ("ashby", "untouched") in by_key, "an unprobed prior row must survive a capped run"
    assert [(r["provider"], r["slug"].casefold()) for r in rows] == sorted(
        (r["provider"], r["slug"].casefold()) for r in rows
    ), "rows must be sorted for stable diffs"

    public = json.loads(to_jsonl(rows, PUBLIC_FIELDS).splitlines()[0])
    assert set(public) == set(PUBLIC_FIELDS), public
    assert "job_count" not in public and "first_seen" not in public, "§6.6 privacy projection"
    assert gzip.decompress(gzip_bytes(b"x" * 100)) == b"x" * 100
    assert gzip_bytes(b"abc") == gzip_bytes(b"abc"), "gzip output must be deterministic"

    degraded = Stats(ok=20, zero_jobs=19)
    assert degraded.degraded and not Stats(ok=20, zero_jobs=5).degraded

    print("selfcheck ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cap", type=int, default=400, help="max candidates probed per provider")
    parser.add_argument("--providers", default=",".join(PROVIDERS))
    parser.add_argument("--concurrency", type=int, default=10, help="§6.3: 10 concurrent overall")
    parser.add_argument("--rate", type=float, default=1.0, help="§6.3: requests/second per host")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--prefixes", default="".join(DEFAULT_PREFIXES))
    parser.add_argument("--wayback-rows", type=int, default=40000)
    parser.add_argument("--no-wayback", action="store_true", help="cheap sources only")
    parser.add_argument("--dry-run", action="store_true", help="probe and report, write nothing")
    parser.add_argument("--out-seed", type=Path, default=SEED_PATH)
    parser.add_argument("--out-public", type=Path, default=PUBLIC_PATH)
    parser.add_argument("--selfcheck", action="store_true", help="offline asserts, no network")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request line at INFO. A 3,000-probe sweep would bury this script's
    # own six lines of progress under 3,000 URLs, each carrying a company slug.
    logging.getLogger("httpx").setLevel(logging.WARNING if not args.verbose else logging.INFO)
    if args.selfcheck:
        return selfcheck()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
