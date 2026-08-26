"""Greenhouse adapter (SPEC v2 §5.1).

One request per board::

    GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true&pay_transparency=true

**No pagination** — verified `meta.total == len(jobs)` — and **no detail call**:
`?content=true` inlines the ad body, HTML-entity-escaped, so it is unescaped exactly once
here. Everything downstream of the field mapping (location, remote, employment, salary,
dates, redaction, identity) belongs to :func:`core.normalize.record.build_job_record`; this
module only decides *which Greenhouse key* feeds *which unified field*.

The three v1 corrections of §5.1 are enforced here, each on measured data:

1. `url` and `applyUrl` are both `absolute_url` verbatim — no `#app` anchor exists in the
   payload, and the host is usually the customer's own domain (stripe 25/25 `stripe.com`
   in the committed fixture).
2. `team` is always null and `departments[0].name` is stripped of its leading org code
   (`"1653 Startups - Account Executives (NA)"` -> `"Account Executives (NA)"`); every job
   on every sampled board has exactly one department.
3. `offices[].name` never reaches `locations[]` — those objects are org rollup buckets
   (`{"name": "US", "location": null}`), and "Japan Locations" is not a place. Only the
   non-null `offices[].location` strings are used.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from core.http import MAX_RESPONSE_BYTES
from core.models import JobRecord, ProviderSpec, Ref
from core.normalize.dates import pick_date
from core.normalize.html import unescape_once
from core.normalize.record import build_job_record

SPEC = ProviderSpec(name="greenhouse", host_rate_limit=2.0, needs_detail_call=False)

API_BASE = "https://boards-api.greenhouse.io/v1/boards"

#: §5.1: `content=true` on a 5,000-job board can exceed 30 MB. Past this the board is
#: re-requested without the descriptions and every row carries :data:`SIZE_WARNING`.
#:
#: Derived from the transport's own cap rather than written out, because it has to stay
#: *below* it: at a flat 40 MB against a 24 MB `MAX_RESPONSE_BYTES` (V3 S20) this guard
#: was unreachable — `read_capped` raised `parse_error` first and the graceful
#: re-fetch-without-descriptions path was dead code.
MAX_BODY_BYTES = MAX_RESPONSE_BYTES * 2 // 3
SIZE_WARNING = "description_omitted_size"

#: §5.1 correction 2. The spec's own regex (`^\s*\d{2,6}\s*[-–—]\s*`) only covers
#: `"1653 - Team"`, but no live department has that shape: Stripe's are
#: `"1653 Startups - Account Executives (NA)"` and `"1195 Account Executives (APAC)"`.
#: This matches the org code plus the optional bucket name up to the first dash, which is
#: what §10.1's expected value requires. Anchored on 2-6 leading digits, so an ordinary
#: department name ("Engineering", "3D Hardware") is never touched.
_ORG_CODE = re.compile(r"^\s*\d{2,6}\b\s*(?:[^-–—]{0,40}?\s*[-–—])?\s*")


def normalize_department(name: object) -> str | None:
    """Strip Greenhouse's internal org code off a department name (§5.1 correction 2).

    Without this, `department`, the `departments` input filter and §7's `by_dept`
    aggregation fragment one company into hundreds of singleton departments.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    stripped = _ORG_CODE.sub("", name).strip()
    return stripped or name.strip()


def _dicts(value: object) -> list[dict[str, Any]]:
    """The nested Greenhouse arrays, defensively: absent, null and `[{}]` all survive."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _board(response: httpx.Response) -> list[dict[str, Any]]:
    """Decode one board response. A payload without a `jobs[]` array is `parse_error`
    (§5.12) rather than a silently empty company."""
    payload = response.json()
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("Greenhouse board payload carries no jobs[] array")
    return [job for job in jobs if isinstance(job, dict)]


async def fetch(ref: Ref, client: Any, *, options: dict[str, Any] | None = None) -> list[JobRecord]:
    """Every job on one Greenhouse board (§5.1). Failures raise `core.http.FetchError`.

    404 -> `not_found` and a malformed body -> `parse_error` both come from the shared
    client, so the §5.12 retry table is applied once for all six providers.
    """
    url = f"{API_BASE}/{ref.slug}/jobs"
    oversize = False

    def parse(response: httpx.Response) -> list[dict[str, Any]]:
        nonlocal oversize
        # Decompressed size: Greenhouse honours gzip, so `content-length` is the 6x
        # smaller compressed figure and would never trip the cap.
        if len(response.content) > MAX_BODY_BYTES:
            oversize = True
            return []
        return _board(response)

    jobs = await client.get_json(
        url, params={"content": "true", "pay_transparency": "true"}, parse=parse
    )

    warnings: list[str] = []
    if oversize:
        warnings.append(SIZE_WARNING)
        jobs = await client.get_json(url, params={"pay_transparency": "true"}, parse=_board)

    return [to_record(job, ref, options, warnings=warnings) for job in jobs]


def to_record(
    raw: dict[str, Any],
    ref: Ref,
    options: dict[str, Any] | None = None,
    *,
    warnings: list[str] | None = None,
) -> JobRecord:
    """One board job -> one :class:`~core.models.JobRecord` (§5.1 mapping table, §4.5).

    `remote` and `employmentType` are deliberately absent from the mapping: Greenhouse
    reports neither, so they are inferred (or left null) by §4.5.2 and §4.5.4 — which is
    the honesty §4.6 sells.
    """
    job = raw if isinstance(raw, dict) else {}
    location = job.get("location") if isinstance(job.get("location"), dict) else {}
    departments = _dicts(job.get("departments"))
    offices = _dicts(job.get("offices"))
    absolute_url = job.get("absolute_url")
    content = job.get("content")

    posted_at, posted_source = pick_date(
        ("first_published", job.get("first_published")),
        ("updated_at", job.get("updated_at")),
    )

    extracted: dict[str, Any] = {
        "job": job,
        "sourceId": job.get("id"),
        "title": job.get("title"),
        "company": job.get("company_name"),
        "department": normalize_department(departments[0].get("name") if departments else None),
        "team": None,  # §5.1 correction 2: `departments[1]` exists on no live job.
        "locationRaw": location.get("name"),
        # §5.1 correction 3: office *names* are org buckets, not places.
        "locations": [office.get("location") for office in offices if office.get("location")],
        "url": absolute_url,
        "applyUrl": absolute_url,  # §5.1 correction 1: same value, no `#app` anchor.
        "postedAt": posted_at,
        "postedAtSource": posted_source,
        "updatedAt": job.get("updated_at"),
        "descriptionHtml": unescape_once(content) if isinstance(content, str) else None,
        "requisitionId": job.get("requisition_id"),
        "warnings": list(warnings or []),
    }
    return build_job_record(ref, extracted, options)
