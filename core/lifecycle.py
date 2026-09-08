"""Job-posting lifecycle enrichment: somebody else's job URLs in, our history out.

This is the read side of the §7 history store, shaped for Apify's Actor-to-Actor
integration. An upstream Actor produces a dataset of job rows; the platform hands us its
dataset id; we answer, per row, how long that posting has been up and whether we have
seen it at all.

**What this can and cannot say.** Our history begins 2026-08-26 and covers the boards on
the watchlist. Everything here is therefore left-censored and partial, and the row says so
rather than implying otherwise:

* ``firstSeen`` is the first day *we* saw the posting, never the day it was created. On
  2026-09-05, 5,300 of 5,915 recorded removals (90%) were for postings the board says
  predate our first sweep, so the censored case is the common one, not the edge.
* ``postedAt`` is the board's own date when it publishes one, and that *is* the real
  posting date. Where both exist, ``postedAt`` is the honest age and ``firstSeen`` is
  merely when we started watching.
* A board added to the watchlist yesterday can only say "not removed in one day".
  ``historyWindowDays`` is what stops that reading as "this posting never closes".

**Why only Lever, Ashby and Rippling.** Identity has to be exact or the answer belongs to
a different job. Those three put the board API's own key in the posting URL path, verified
2026-09-05:

    lever      jobs.lever.co/{company}/{uuid}          uuid == api id
    ashby      jobs.ashbyhq.com/{company}/{uuid}       uuid == jobs[].id
    rippling   ats.rippling.com/{company}/jobs/{uuid}  uuid == api uuid

Greenhouse redirects to the employer's own domain with the id in a ``gh_jid`` query
parameter, so the URL shape differs per company; Recruitee URLs carry a mutable title slug
and no id at all. Both are reported as unsupported rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

#: Providers whose public posting URL carries the board API's own job key.
SUPPORTED = ("lever", "ashby", "rippling")

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

#: One pattern per supported provider. Anchored on the provider's own host so a link
#: shortener or an employer redirect cannot be read as a board URL, and closed after the
#: UUID so `.../{uuid}garbage` is not read as `.../{uuid}` — that resolved to a real
#: posting and would have attached its history to a URL that is not it.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "lever",
        re.compile(rf"^https?://jobs(?:\.eu)?\.lever\.co/([^/?#]+)/({_UUID})(?=$|[/?#])", re.I),
    ),
    ("ashby", re.compile(rf"^https?://jobs\.ashbyhq\.com/([^/?#]+)/({_UUID})(?=$|[/?#])", re.I)),
    (
        "rippling",
        re.compile(rf"^https?://ats\.rippling\.com/([^/?#]+)/jobs/({_UUID})(?=$|[/?#])", re.I),
    ),
)

#: Providers we watch but cannot identify a single posting for from its URL alone.
_UNSUPPORTED_HOSTS = (
    ("greenhouse", re.compile(r"(?:job-boards|boards)\.greenhouse\.io|[?&]gh_jid=", re.I)),
    ("recruitee", re.compile(r"\.recruitee\.com/", re.I)),
    ("personio", re.compile(r"\.jobs\.personio\.(?:com|de)/", re.I)),
)

#: Values of the row's `coverage` field. Exactly one is true of any given row, and only
#: `tracked` carries lifecycle facts — the others exist so a caller can tell *why* a row
#: is empty instead of reading emptiness as "this job never closed".
COVERAGE_TRACKED = "tracked"  # board is on the watchlist and the id was found
COVERAGE_BOARD_UNTRACKED = "board_untracked"  # we have never watched this board
COVERAGE_JOB_UNKNOWN = "job_unknown"  # board watched, this id is not in today's state
COVERAGE_UNSUPPORTED = "unsupported_provider"  # provider we cannot identify from a URL
COVERAGE_UNRESOLVED = "unresolved_url"  # not a job URL we recognise at all

#: Age bases that are an age rather than a bound on one. See `is_chargeable`.
CHARGEABLE_BASES = frozenset({"board", "observed"})


@dataclass(slots=True)
class Ref:
    """A posting URL resolved to the identity the history store is keyed by."""

    provider: str
    company: str
    job_id: str


def parse_posting_url(url: object) -> Ref | None:
    """`Ref` for a supported provider's posting URL, else None.

    Deliberately strict: the company segment is taken from the URL path and the id must be
    a UUID in the position that provider puts it. A near-miss returns None and becomes an
    ``unresolved_url`` row, because a wrong identity produces a confidently wrong lifecycle.
    """
    if not isinstance(url, str):
        return None
    text = url.strip()
    for provider, pattern in _PATTERNS:
        match = pattern.match(text)
        if match:
            return Ref(provider=provider, company=match.group(1), job_id=match.group(2))
    return None


def unsupported_provider(url: object) -> str | None:
    """Name the provider for a URL we recognise but cannot resolve to one posting."""
    if not isinstance(url, str):
        return None
    for provider, pattern in _UNSUPPORTED_HOSTS:
        if pattern.search(url):
            return provider
    return None


def first_url(row: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    """First non-empty string among `fields`, then any value that looks like a posting URL.

    Upstream Actors name this column differently — `url`, `jobUrl`, `applyUrl`,
    `absolute_url`, `hostedUrl`. Taking the configured field first keeps the caller in
    control; the sweep is what stops a working integration breaking on a rename.

    A field that resolves wins over one that merely has text in it. Datasets carry a
    `url` holding the company's careers page alongside a `jobUrl` holding the posting;
    returning the first *non-empty* field made every such row `unresolved_url` while the
    answer sat in the next column.
    """
    named = [row.get(n) for n in fields]
    for value in named:
        if isinstance(value, str) and parse_posting_url(value):
            return value.strip()
    for value in row.values():
        if isinstance(value, str) and parse_posting_url(value):
            return value.strip()
    for value in named:  # nothing resolves; hand back what the caller pointed at anyway,
        if isinstance(value, str) and value.strip():  # so the row reports *its* URL
            return value.strip()
    return None


def _days_between(earlier: str | None, later: date) -> int | None:
    """Days from `earlier` to `later`, or None if that is not a fact.

    A date in the future is not an age. A board that publishes one — a scheduled posting,
    a timezone slip, a typo in a feed — would otherwise produce `ageDays: -2` presented
    with the same confidence as a real number.
    """
    if not isinstance(earlier, str) or len(earlier) < 10:
        return None
    try:
        days = (later - date.fromisoformat(earlier[:10])).days
    except ValueError:
        return None
    return days if days >= 0 else None


#: Where the published index lives. An Apify Actor runs under *the caller's* account and
#: cannot read our key-value store — that closed this path once. A public file has no
#: permission model to get wrong.
INDEX_URL = "https://moonie0201.github.io/hiring-closures/age-index.json.gz"


#: Index layouts this reader understands. The publisher and this function agree on a
#: positional row by convention, and convention is exactly what drifts: swapping `posted`
#: and `first_seen` in the writer produces a plausible age here and nothing complains.
SUPPORTED_SCHEMAS = (1,)


class IndexError_(ValueError):
    """The published index is not one this build can read. Refusing is the whole point."""


def load_index(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`{"provider:company": {"t": tracked_since, "j": {job_id: (posted, first_seen)}}}`.

    The file nests jobs under boards so the slug is not repeated 138,813 times; this flips
    it into the lookup the enricher actually makes.

    Raises on a schema it does not know and on a duplicated board, because both fail
    silently otherwise — a version bump would swap two date columns and still return
    confident ages, and a duplicate key would drop one board's jobs without a word.
    """
    version = payload.get("schema")
    if version not in SUPPORTED_SCHEMAS:
        raise IndexError_(f"index schema {version!r}; this build reads {SUPPORTED_SCHEMAS}")

    boards: dict[str, dict[str, Any]] = {}
    for board in payload.get("boards") or []:
        key = f"{board.get('p')}:{board.get('c')}"
        if key in boards:
            raise IndexError_(f"duplicate board {key!r} in the index")
        jobs = {}
        for row in board.get("j") or []:
            if not isinstance(row, list) or len(row) != 3:
                raise IndexError_(f"board {key!r} has a row of {len(row) if row else 0} fields")
            jobs[row[0]] = (row[1], row[2])
        boards[key] = {"t": board.get("t"), "j": jobs}
    return boards


@dataclass(slots=True)
class Enricher:
    """Answers from the published age index, held in memory for the run.

    A live posting needs only its board's entry: the index carries the board's own posted
    date and our first sighting per job. 3.3 MB gzipped for 138,813 jobs, measured
    2026-09-08, so one fetch covers a whole run however many rows it has.
    """

    boards: dict[str, dict[str, Any]]
    today: date
    generated: str | None = None

    def enrich(self, url: str | None) -> dict[str, Any]:
        """Lifecycle facts for one posting URL. Never raises; unknowns are stated."""
        base: dict[str, Any] = {
            "sourceUrl": url,
            "provider": None,
            "company": None,
            "jobId": None,
            "coverage": COVERAGE_UNRESOLVED,
            "historyTrackedSince": None,
            "historyWindowDays": None,
            "firstSeen": None,
            "firstSeenCensored": None,
            "postedAt": None,
            "ageDays": None,
            "ageBasis": None,
            "stillListed": None,
        }
        if url is None:
            return base

        ref = parse_posting_url(url)
        if ref is None:
            provider = unsupported_provider(url)
            if provider:
                base["provider"] = provider
                base["coverage"] = COVERAGE_UNSUPPORTED
            return base

        base["provider"], base["company"], base["jobId"] = ref.provider, ref.company, ref.job_id
        entry = self.boards.get(f"{ref.provider}:{ref.company}")
        if entry is None:
            base["coverage"] = COVERAGE_BOARD_UNTRACKED
            return base

        tracked_since = entry.get("t")
        base["historyTrackedSince"] = tracked_since
        base["historyWindowDays"] = _days_between(tracked_since, self.today)

        found = (entry.get("j") or {}).get(ref.job_id)
        job = {"posted": found[0], "first_seen": found[1]} if found else None
        if job is None:
            # The board is watched and this id is not on it today. That is not the same as
            # "removed": we may have started watching after it closed, or the upstream row
            # may be stale. Saying `job_unknown` and leaving `stillListed` null is the
            # difference between a fact and an inference.
            base["coverage"] = COVERAGE_JOB_UNKNOWN
            return base

        first_seen = job.get("first_seen")
        posted = job.get("posted")
        base["coverage"] = COVERAGE_TRACKED
        base["firstSeen"] = first_seen
        base["postedAt"] = posted
        base["stillListed"] = True

        # Censored when our first sighting is the day we started watching that board: the
        # posting was already up, so `firstSeen` is a floor on its age, not its age.
        base["firstSeenCensored"] = bool(
            first_seen and tracked_since and first_seen <= tracked_since
        )

        # The board's own date beats ours whenever it publishes one.
        if posted:
            base["ageDays"] = _days_between(posted, self.today)
            base["ageBasis"] = "board"
        elif first_seen:
            base["ageDays"] = _days_between(first_seen, self.today)
            base["ageBasis"] = "observed_floor" if base["firstSeenCensored"] else "observed"
        return base


def is_chargeable(row: dict[str, Any]) -> bool:
    """Only a row that actually delivers a lifecycle fact earns a charge.

    Resolution failures, unsupported providers, untracked boards and unknown ids are all
    real answers and all free — charging for "we do not know" is how a data product loses
    the trust it sells.

    `observed_floor` is free for the same reason. It says the posting is *at least* this
    old because it was already up on the day we started watching the board; on a board we
    began watching yesterday that reads as "1 day" and is worth nothing. Measured against
    the 2026-09-08 index, no row takes this branch — all 138,813 carry the board's own
    posted date — so this costs nothing today and is here for the provider that stops
    publishing one.
    """
    return (
        row.get("coverage") == COVERAGE_TRACKED
        and row.get("ageDays") is not None
        and row.get("ageBasis") in CHARGEABLE_BASES
    )
