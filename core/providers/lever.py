"""Lever adapter (SPEC v2 §5.2).

``GET https://api.lever.co/v0/postings/{site}?mode=json`` — public, unauthenticated, one
request per board, no detail call: ``description``, ``lists[]`` and ``additional`` are all
inline. The EU tenant lives on a second host, ``api.eu.lever.co``, and which host a board
is on is not knowable from the slug — see :func:`list_jobs`.

Three provider facts drive everything below:

* **Case-sensitive slugs.** ``palantir`` is 200, ``Palantir`` is 404 (§5.11). ``ref.slug``
  keeps the directory's casing and is used verbatim; nothing here lower-cases it.
* **1 rps.** ``api.lever.co/robots.txt`` is ``Allow: /`` + ``Crawl-delay: 1``. The cap is
  enforced per host by :mod:`core.http`, not here, because every Lever tenant shares the
  two hosts (§5.12).
* **``ai_train=False``.** ``jobs.lever.co`` carries the Content-Signal Art. 4 reservation;
  ``api.lever.co`` does not. We honour it anyway, by field rather than by memory (§5, V1 L8).

Everything that is not Lever-shaped — location, remote, employment, salary, dates,
redaction, identity — belongs to :func:`core.normalize.record.build_job_record` and is not
reimplemented here.
"""

from __future__ import annotations

from typing import Any

import httpx

from core.http import Client, NotFound
from core.models import JobRecord, Meta, ProviderSpec, Ref
from core.normalize.location import country_name
from core.normalize.record import build_job_record

SPEC = ProviderSpec(
    name="lever",
    host_rate_limit=1.0,  # robots.txt Crawl-delay: 1
    needs_detail_call=False,
    ai_train=False,  # §5, V1 L8
    retainable=True,
)

GLOBAL_HOST = "api.lever.co"
EU_HOST = "api.eu.lever.co"

#: `skip`/`limit` are verified working; the default response is already complete, so the
#: loop below only issues a second request when a page comes back exactly full (§5.2).
PAGE_SIZE = 1000


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _postings(response: httpx.Response) -> list[dict[str, Any]]:
    """Decode one page. Anything that is not a JSON array is a malformed body.

    Raising here rather than after :meth:`core.http.Client.get_json` returns puts the
    check inside that method's retry, so a truncated body is retried once and only then
    becomes ``parse_error`` (§5.12).
    """
    data = response.json()
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON array of postings, got {type(data).__name__}")
    return [job for job in data if isinstance(job, dict)]


async def _postings_for(client: Client, host: str, slug: str) -> list[dict[str, Any]]:
    url = f"https://{host}/v0/postings/{slug}"
    jobs: list[dict[str, Any]] = []
    skip = 0
    while True:
        page = await client.get_json(
            url, params={"mode": "json", "limit": PAGE_SIZE, "skip": skip}, parse=_postings
        )
        jobs.extend(page)
        if len(page) < PAGE_SIZE:
            return jobs
        skip += PAGE_SIZE


async def list_jobs(ref: Ref, client: Client) -> tuple[list[dict[str, Any]], Meta]:
    """Raw postings for one board, probing global then EU (§5.2, corrected in V2 T-H6).

    The measured behaviour: ``api.lever.co/v0/postings/lever`` answers **200 with ``[]``**
    while ``api.eu.lever.co`` serves that board's 6 jobs. Probing EU only on 404 — v1's
    rule — would therefore have made every EU-hosted tenant permanently invisible and
    cached the wrong host. So:

    * a non-empty array on either host wins, and the winning region is written back to
      ``ref.region`` so the directory can cache it;
    * 200-with-``[]`` on both hosts is ``ok`` with 0 jobs and caches **no** region — the
      board may fill later on either one;
    * 404 on both is :class:`core.http.NotFound` -> ``not_found``. Verified for
      ``wise``/``n26``/``bolt``/``personio``/``netflix``.

    A cached ``region="eu"`` reverses the probe order, so a known-EU board costs one
    request instead of two.
    """
    hosts = (
        (EU_HOST, GLOBAL_HOST) if (ref.region or "").casefold() == "eu" else (GLOBAL_HOST, EU_HOST)
    )
    missing: NotFound | None = None
    found = 0
    for host in hosts:
        try:
            jobs = await _postings_for(client, host, ref.slug)
        except NotFound as exc:
            missing = exc
            continue
        found += 1
        if jobs:
            ref.region = "eu" if host == EU_HOST else None
            return jobs, Meta(total=len(jobs))
    if not found and missing is not None:
        raise missing
    return [], Meta(total=0)


def _description_html(job: dict[str, Any]) -> str | None:
    """``description`` + ``lists[].content`` + ``additional`` (§5.2).

    ``description`` already contains ``opening`` + ``descriptionBody``, so those two are
    deliberately not concatenated a second time (V2 T-L4).
    """
    parts = [_text(job.get("description"))]
    lists = job.get("lists")
    if isinstance(lists, list):
        parts += [_text(_dict(block).get("content")) for block in lists]
    parts.append(_text(job.get("additional")))
    return "\n".join(p for p in parts if p) or None


def _description_text(job: dict[str, Any]) -> str | None:
    """``descriptionPlain`` + ``additionalPlain`` (§5.2)."""
    parts = (_text(job.get("descriptionPlain")), _text(job.get("additionalPlain")))
    return "\n".join(p for p in parts if p) or None


def _apply_country(record: JobRecord, value: object) -> None:
    """Top-level ``country`` (ISO2, e.g. ``"GB"``) overrides the parsed code (§5.2).

    Only the flat record fields are touched. ``locations[]`` keeps what each location
    string itself parsed to: the posting-level country describes the primary location, and
    rewriting the list members would also invalidate the §4.5.1 step 8 sort they arrived in.
    ``country`` is re-derived from the code so the pair is never half-populated (§4.5.1).
    """
    code = (_text(value) or "").upper()
    if not code:
        return
    record.countryCode = code
    record.country = country_name(code) or record.country


def to_record(raw: dict[str, Any], ref: Ref, options: dict[str, Any] | None = None) -> JobRecord:
    """One Lever posting -> :class:`~core.models.JobRecord` (§5.2 mapping table, §4.5).

    Every provider object is read through ``.get()`` chains: ``categories`` missing, empty
    or the wrong type must produce nulls, not an exception (§10.1
    ``test_adapters_empty_objects``).
    """
    job = _dict(raw)
    categories = _dict(job.get("categories"))
    all_locations = job.get("allLocations") or categories.get("allLocations")

    record = build_job_record(
        ref,
        {
            # `job` carries the ATS flags the shared normalizers read directly:
            # `workplaceType` (§4.5.2 rank 1) and `salaryRange` (§4.5.3 step 1).
            "job": job,
            "sourceId": job.get("id"),
            "title": job.get("text"),
            # `department` falls back to `team`: Lever tenants routinely fill only one,
            # and a null department fragments every §7 `by_dept` aggregation.
            "department": _text(categories.get("department")) or _text(categories.get("team")),
            "team": categories.get("team"),
            "locationRaw": categories.get("location"),
            "locations": all_locations if isinstance(all_locations, list) else [],
            "employmentType": categories.get("commitment"),
            "salaryText": job.get("salaryDescription"),
            "url": job.get("hostedUrl"),
            "applyUrl": job.get("applyUrl"),
            "postedAt": job.get("createdAt"),  # epoch ms
            "postedAtSource": "createdAt",
            "descriptionHtml": _description_html(job),
            "descriptionText": _description_text(job),
        },
        options,
    )
    _apply_country(record, job.get("country"))
    return record


async def fetch(ref: Ref, client: Client, options: dict[str, Any] | None = None) -> list[JobRecord]:
    """Every posting for one Lever board, normalized (§5.2)."""
    jobs, _meta = await list_jobs(ref, client)
    return [to_record(job, ref, options) for job in jobs]
