"""Rippling job-board API v2 adapter (SPEC v2 §5.7).

    list   GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs
    detail GET .../board/{slug}/jobs/{uuid}

Two things make Rippling unlike the other five providers:

1. **The list endpoint returns one row per (job × location)** — 745 rows for 377 uuids on
   its own board. The adapter groups by ``uuid`` and merges the ``workLocation`` labels
   into ``locations[]`` **before** anything downstream can count or charge for a row
   (§5.7, §4.5.6).
2. **The detail call is mandatory** — the list row carries only
   ``uuid, name, department, url, workLocation``, so ``employmentType``, ``createdOn``,
   ``payRangeDetails`` and the description exist only on the detail response. That costs
   1 + N requests per company; ``outputProfile: "minimal"`` and the §7.3 history snapshot
   skip it and leave those fields null.

Everything that is not provider-specific — location, remote, employment, salary, dates,
redaction, identity — is left to :func:`core.normalize.record.build_job_record`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.http import Client, FetchError, ParseError
from core.models import JobRecord, ProviderSpec, Ref
from core.normalize.location import parse_location
from core.normalize.record import build_job_record

SPEC = ProviderSpec(name="rippling", host_rate_limit=2.0, needs_detail_call=True)

LIST_URL = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"

#: §4.5.5 — ``postedAt`` comes from the detail response's ``createdOn`` and nowhere else.
POSTED_AT_SOURCE = "createdOn"


def _str(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _location_labels(job: dict[str, Any], extra: list[str] | None) -> list[str]:
    """Every location string for one uuid, deduped and sorted by §4.5.1 step 8.

    Sorting here (with the same parser §4.5.1 uses) is what makes ``locationRaw`` "the
    first ``workLocation.label`` **after sorting**" rather than whichever row the list
    endpoint happened to emit first — the ordering the whole `changeHash` story rests on.
    """
    labels: list[str] = []
    for value in list(extra or []) + list(job.get("workLocations") or []):
        label = _str(value)
        if label and label not in labels:
            labels.append(label)
    label = _str(_dict(job.get("workLocation")).get("label"))
    if label and label not in labels:
        labels.append(label)
    return sorted(labels, key=lambda text: parse_location(text).sort_key)


def _description_html(value: object) -> str | None:
    """Detail ``description`` is ``{"company": html, "role": html}`` — sections, in the
    order the provider sent them."""
    if isinstance(value, dict):
        value = "\n".join(part for part in value.values() if isinstance(part, str))
    return _str(value)


def _pay_aliased(job: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``job`` whose ``payRangeDetails`` bands also carry ``min``/``max``.

    Rippling names its band bounds ``rangeStart``/``rangeEnd``; §4.5.3's structured
    reader looks for ``min``/``max``. Renaming a provider's field is adapter work, so the
    alias is added here — alongside the provider's own keys, never replacing them — and
    the pristine payload is put back on ``record.raw`` in :func:`to_record`.
    """
    bands = job.get("payRangeDetails")
    if not isinstance(bands, list) or not bands:
        return job
    aliased = [
        {**band, "min": band.get("rangeStart"), "max": band.get("rangeEnd")}
        if isinstance(band, dict) and "rangeStart" in band and "min" not in band
        else band
        for band in bands
    ]
    return {**job, "payRangeDetails": aliased}


def to_record(
    raw: dict[str, Any],
    ref: Ref,
    options: dict[str, Any] | None = None,
    *,
    locations: list[str] | None = None,
    warnings: list[str] | None = None,
) -> JobRecord:
    """One merged Rippling job (``{**list_row, **detail}``) -> :class:`JobRecord`.

    ``locations`` carries the ``workLocation`` labels from the *other* list rows that
    share this uuid. Every nested object is read through ``.get()`` chains: the detail
    ``department`` has no ``label`` key and the list ``department`` has no
    ``department_tree`` (V2 T-C3), so either shape has to survive being absent.
    """
    job = _dict(raw)
    department = _dict(job.get("department"))
    tree = [t for t in (_str(v) for v in department.get("department_tree") or []) if t]
    labels = _location_labels(job, locations)

    record = build_job_record(
        ref,
        {
            "job": _pay_aliased(job),
            "sourceId": job.get("uuid"),
            "title": job.get("name"),
            "company": job.get("companyName") or _dict(job.get("board")).get("title"),
            # §5.7: department_tree[0] -> base_department -> the list row's label.
            "department": tree[0]
            if tree
            else department.get("base_department") or department.get("label"),
            # §5.7: the leaf of the tree, or the detail object's own name.
            "team": tree[-1] if len(tree) > 1 else department.get("name"),
            "locationRaw": labels[0] if labels else None,
            "locations": labels,
            "employmentType": job.get("employmentType"),
            "url": job.get("url"),
            "postedAt": job.get("createdOn"),
            "postedAtSource": POSTED_AT_SOURCE,
            "descriptionHtml": _description_html(job.get("description")),
            "warnings": warnings or [],
        },
        options,
    )
    if record.raw is not None:
        record.raw = job  # the payload as the provider sent it, without the pay aliases
    return record


async def _detail(client: Client, list_url: str, uuid: str) -> dict[str, Any] | None:
    """The detail payload, or ``None`` when this one call failed.

    §5.12: a detail failure for a single job does not fail the company — the job is
    emitted with description and salary null and a ``detail_failed`` warning.
    """
    try:
        payload = await client.get_json(f"{list_url}/{uuid}")
    except FetchError:
        return None
    return payload if isinstance(payload, dict) else None


async def fetch(
    ref: Ref,
    client: Client,
    options: dict[str, Any] | None = None,
) -> list[JobRecord]:
    """Every listed job for one Rippling board.

    A 404 on the list call is `not_found`, a 4xx/5xx is `http_error` and a body that is
    not a JSON array is `parse_error` — all raised by :mod:`core.http` or here, never
    swallowed, because the run summary distinguishes them (§5.12).
    """
    options = options or {}
    url = LIST_URL.format(slug=ref.slug)
    rows = await client.get_json(url)
    if not isinstance(rows, list):
        raise ParseError(f"expected a JSON array of jobs, got {type(rows).__name__}", url=url)

    # Group by uuid *before* anything counts rows: the 48% row duplication is (job x
    # location), not 48% more jobs, and charging on rows would bill for it (§5.7, §4.5.6).
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        uuid = _str(_dict(row).get("uuid"))
        if uuid:
            groups.setdefault(uuid, []).append(row)

    list_only = bool(options.get("listOnly")) or options.get("outputProfile") == "minimal"
    details: list[dict[str, Any] | None] = (
        [None] * len(groups)
        if list_only
        else list(await asyncio.gather(*(_detail(client, url, uuid) for uuid in groups)))
    )

    records: list[JobRecord] = []
    for uuid_rows, detail in zip(groups.values(), details, strict=True):
        # Defensive guard, same status as Ashby's `isListed` (V2 T-M8): one sampled job
        # has `unlistedFromSearch: false` and whether `true` occurs is unverified.
        if _dict(detail).get("unlistedFromSearch") is True:
            continue
        records.append(
            to_record(
                {**_dict(uuid_rows[0]), **_dict(detail)},
                ref,
                options,
                locations=[
                    label
                    for label in (
                        _str(_dict(_dict(r).get("workLocation")).get("label")) for r in uuid_rows
                    )
                    if label
                ],
                warnings=[] if (detail is not None or list_only) else ["detail_failed"],
            )
        )
    return records
