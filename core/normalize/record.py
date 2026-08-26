"""``build_job_record()`` — the shared adapter -> ``JobRecord`` path (SPEC v2 §4.5, §9.1).

Every adapter maps its provider's payload onto the flat ``extracted`` dict below and hands
it here; everything that is *not* provider-specific — location, remote, employment, salary,
dates, redaction, identity — happens once, in this order, for all six providers:

    description -> **redact** -> location -> remote -> employment -> salary -> dates -> identity

Redaction runs before salary parsing so a phone number cannot be read as a pay range
(§4.5.3), and before the description is dropped for ``includeDescription: false``, so the
salary regex still sees a redacted body rather than an unredacted one.

``extracted`` keys, all optional except ``sourceId`` and ``title``::

    job                 provider payload; read for ATS remote flags and structured salary
    sourceId title company companyDomain department team
    locationRaw         primary location string
    locations           further location strings (Lever allLocations, Ashby secondary…)
    locationStructured  one dict, or one per location (Ashby postalAddress, Recruitee…)
    employmentType      the provider's own value; a dict is fine (Rippling)
    schedule            Personio's full-time/part-time refinement
    seniority yearsOfExperience requisitionId
    salaryText          free-text pay field (Lever salaryDescription)
    url applyUrl postedAt postedAtSource updatedAt
    descriptionHtml descriptionText
    warnings            e.g. ["detail_failed"] (§5.12)

``options`` is the Actor input dict; only ``includeDescription``, ``descriptionFormat``,
``redactContacts``, ``includeRawJson`` and ``scrapedAt`` are read, with the §4.1 defaults.
"""

from __future__ import annotations

from typing import Any

from core.models import JobRecord, Ref
from core.normalize.dates import to_iso_utc, utc_now_iso
from core.normalize.employment import detect_employment
from core.normalize.html import html_to_text, sanitize_html
from core.normalize.identity import apply_identity
from core.normalize.location import parse_locations
from core.normalize.redact import redact_description, strip_contact_fields
from core.normalize.remote import detect_remote
from core.normalize.salary import parse_salary


def _str(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


def build_job_record(
    ref: Ref,
    extracted: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> JobRecord:
    """Normalize one provider job into a dataset-ready :class:`JobRecord`."""
    options = options or {}
    include_description = bool(options.get("includeDescription", False))
    description_format = options.get("descriptionFormat") or "text"
    redact = bool(options.get("redactContacts", True))
    job = extracted.get("job") or {}

    record = JobRecord(
        provider=ref.provider,
        companySlug=ref.slug,
        company=_str(extracted.get("company")),
        companyDomain=_str(extracted.get("companyDomain")),
        title=_str(extracted.get("title")),
        department=_str(extracted.get("department")),
        team=_str(extracted.get("team")),
        seniority=_str(extracted.get("seniority")),
        yearsOfExperience=_str(extracted.get("yearsOfExperience")),
        url=_str(extracted.get("url")),
        applyUrl=_str(extracted.get("applyUrl")),
        requisitionId=_str(extracted.get("requisitionId")),
        sourceId=_str(extracted.get("sourceId")),
        input=ref.input,
        scrapedAt=options.get("scrapedAt") or utc_now_iso(),
        warnings=list(extracted.get("warnings") or []),
    )

    # 1. Description, then redaction — before anything reads the body (§4.5.3).
    description_html = _str(extracted.get("descriptionHtml"))
    description_text = _str(extracted.get("descriptionText")) or html_to_text(description_html)
    description_html, description_text, redacted = redact_description(
        description_html, description_text, redact
    )
    # V3 S23: applied here, at the one point every provider's body converges, so it cannot
    # be forgotten per-adapter. After redaction, so the sanitiser never sees a contact.
    description_html = sanitize_html(description_html)

    # 2. Location (§4.5.1). ``locations[]`` comes back sorted, so nothing downstream can
    #    forget step 8 and flip ``changeHash`` on a provider reshuffle.
    primary, all_locations = parse_locations(
        _str(extracted.get("locationRaw")),
        [s for s in (_str(v) for v in extracted.get("locations") or []) if s],
        extracted.get("locationStructured"),
    )
    record.apply_location(primary, all_locations)

    # 3. Remote (§4.5.2). Rank 5 is opt-in: the description is only offered when the buyer
    #    asked for descriptions at all.
    record.remote, record.workplaceType, record.remoteSource = detect_remote(
        job,
        primary,
        record.title,
        description_text if include_description else None,
    )

    # 4. Employment type (§4.5.4), with its provenance.
    (
        record.employmentType,
        record.employmentTypeRaw,
        record.employmentTypeSource,
    ) = detect_employment(extracted.get("employmentType"), record.title, extracted.get("schedule"))

    # 5. Salary (§4.5.3) — structured, then regex over the (already redacted) text.
    record.apply_salary(
        parse_salary(job, description_text, primary, _str(extracted.get("salaryText")))
    )

    # 6. Dates (§4.5.5).
    record.postedAt = to_iso_utc(extracted.get("postedAt"))
    record.postedAtSource = _str(extracted.get("postedAtSource")) if record.postedAt else None
    record.updatedAt = to_iso_utc(extracted.get("updatedAt"))

    # 7. Description output shape (§4.1 ``descriptionFormat``).
    if include_description:
        if description_format in ("html", "both"):
            record.descriptionHtml = description_html
        if description_format in ("text", "both"):
            record.descriptionText = description_text
        record.descriptionRedacted = redacted if (description_html or description_text) else None
    if options.get("includeRawJson") and isinstance(job, dict):
        # §15.2: never the untouched payload — see `strip_contact_fields` (V1 B1, V3 S4).
        record.raw = strip_contact_fields(job, redact=redact)

    # 8. Identity and change detection (§4.5.6).
    return apply_identity(record)
