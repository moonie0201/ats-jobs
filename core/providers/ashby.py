"""Ashby adapter (SPEC v2 §5.3).

One public GET per company::

    GET https://api.ashbyhq.com/posting-api/job-board/{jobBoardName}?includeCompensation=true

No auth, no pagination (verified: 754 jobs for openai in one body), no detail call —
`descriptionHtml`, `descriptionPlain`, `compensation` and the structured
`address.postalAddress` are all inline. `includeCompensation=true` is not optional for us:
without it the provider that gives the cleanest structured pay of the six gives none.

Everything after the field mapping — location, remote, employment, salary, dates,
redaction, identity — is :func:`core.normalize.record.build_job_record`, so this module is
only the translation table §5.3 specifies.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from core.http import Client, ParseError
from core.models import JobRecord, Meta, ProviderSpec, Ref
from core.normalize.record import build_job_record

#: Rate cap undocumented by Ashby → the §5.12 default of 2 rps. `api.ashbyhq.com` is one
#: shared host for every company, which is why the cap lives in :class:`core.http.Client`.
SPEC = ProviderSpec(name="ashby", host_rate_limit=2.0, needs_detail_call=False)

LIST_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
LIST_PARAMS: dict[str, str] = {"includeCompensation": "true"}

#: §4.5.5 — the field `postedAt` actually came from, exported as `postedAtSource`.
POSTED_AT_SOURCE = "publishedAt"


def _text(value: Any) -> str | None:
    """Mirror of `build_job_record`'s own string coercion, so the location list this
    adapter builds and the one `record.py` filters stay index-aligned."""
    return (value.strip() or None) if isinstance(value, str) else None


def _postal(address: Any) -> dict[str, Any] | None:
    """`address.postalAddress` when it carries anything; `{}` and absent both → None."""
    if isinstance(address, dict):
        postal = address.get("postalAddress")
        if isinstance(postal, dict) and postal:
            return postal
    return None


def _locations(job: dict[str, Any]) -> tuple[str | None, list[str], list[dict[str, Any]] | None]:
    """`(locationRaw, further location strings, structured parts)` — §5.3, §4.5.1 step 1.

    `[location] + secondaryLocations[].location`, each paired with its own
    `address.postalAddress`. Only text-bearing entries are handed on, because
    `build_job_record` drops empty strings from `locations` and a dropped string would
    shift every structured part onto the wrong location. When nothing has text at all the
    structured parts are passed alone, which is the path `parse_locations` handles for a
    job that has an address and no location line.

    ponytail: alignment assumes one location string per entry. §4.5.1 step 2 can split one
    string into several — measured 0 times across 932 live postings on openai, ramp, linear
    and posthog. Ceiling: a board that writes `"Tokyo, Japan; Singapore"` in one
    `location` gets the primary's country attached to the split parts.
    """
    secondary = job.get("secondaryLocations")
    entries: list[tuple[str | None, dict[str, Any] | None]] = [
        (_text(job.get("location")), _postal(job.get("address")))
    ]
    entries += [
        (_text(item.get("location")), _postal(item.get("address")))
        for item in (secondary if isinstance(secondary, list) else [])
        if isinstance(item, dict)
    ]

    kept = [(text, struct) for text, struct in entries if text]
    if not kept:
        kept = [(None, struct) for _, struct in entries if struct]
    if not kept:
        return None, [], None

    structs = [struct or {} for _, struct in kept]
    return (
        kept[0][0],
        [text for text, _ in kept[1:] if text],
        structs if any(structs) else None,
    )


def to_record(raw: dict[str, Any], ref: Ref, options: dict[str, Any] | None = None) -> JobRecord:
    """One Ashby posting → one `JobRecord` (§5.3 field mapping, §4.5 normalization).

    `company` stays null: the public posting API returns `{"jobs": [...], "apiVersion": …}`
    and no board or company name anywhere, and inventing one from the slug would be a
    guess the rest of this spec refuses to make. `companySlug` carries the slug.
    """
    location_raw, other_locations, structured = _locations(raw)
    return build_job_record(
        ref,
        {
            "job": raw,
            "sourceId": raw.get("id"),
            "title": raw.get("title"),
            "department": raw.get("department"),
            "team": raw.get("team"),
            "locationRaw": location_raw,
            "locations": other_locations,
            "locationStructured": structured,
            # Rank 1 of §4.5.2 reads `isRemote`/`workplaceType` straight off `job`.
            "employmentType": raw.get("employmentType"),
            # `compensation` is read off `job` by §4.5.3's structured step.
            "url": raw.get("jobUrl"),
            "applyUrl": raw.get("applyUrl"),
            "postedAt": raw.get("publishedAt"),
            "postedAtSource": POSTED_AT_SOURCE,
            "descriptionHtml": raw.get("descriptionHtml"),
            "descriptionText": raw.get("descriptionPlain"),
        },
        options,
    )


async def list_jobs(ref: Ref, client: Client) -> tuple[list[dict[str, Any]], Meta]:
    """The one list call (§5.3). Raises `core.http.NotFound` on 404 → `not_found`;
    a truncated body raises `ParseError` after `Client`'s single retry (§5.12).

    The `isListed == false` drop is defensive — measured 0 of 754 on openai — and
    deliberately tests `is False`, so a board that omits the flag is not silently emptied.
    """
    url = LIST_URL.format(slug=quote(ref.slug, safe=""))
    payload = await client.get_json(url, params=LIST_PARAMS)
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        raise ParseError(f"ashby board {ref.slug!r}: no jobs array in the response", url=url)
    listed = [job for job in jobs if isinstance(job, dict) and job.get("isListed") is not False]
    return listed, Meta(total=len(listed))


async def fetch(ref: Ref, client: Client, options: dict[str, Any] | None = None) -> list[JobRecord]:
    """Every listed posting on one Ashby board, normalized."""
    jobs, _meta = await list_jobs(ref, client)
    return [to_record(job, ref, options) for job in jobs]
