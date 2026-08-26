"""Identity, dedupe and change detection (SPEC v2 §4.5.6).

Three keys, three jobs:

* ``id`` — the ATS's own identity. Stable across runs, the dedupe key inside a run and the
  key of the ``onlyNewJobs`` state store.
* ``contentKey`` — "is this the same advertisement?". Includes ``requisitionId`` and the
  **raw** location string, because twelve identical warehouse requisitions in one city are
  twelve real openings and v1's key collapsed them into one.
* ``changeHash`` — "did anything a buyer cares about change?". Every input is canonicalized
  and ``locations[]`` is sorted, so a backend reordering cannot emit a phantom change; the
  description is deliberately excluded so a cosmetic ad edit is not a change.
"""

from __future__ import annotations

import re
from hashlib import sha1

from core.models import JobRecord, Location

#: Legal-entity suffixes stripped from a company name before hashing (§4.5.6). Kept as a
#: regex rather than a `cleanco` dependency — §9.3 keeps the dependency list at three.
_LEGAL_SUFFIX = re.compile(
    r"(?i)[\s,]+(inc|llc|l\.l\.c|ltd|limited|gmbh|mbh|ug|bv|b\.v|nv|ab|oy|oyj|as|a/s|sa|s\.a"
    r"|sas|sarl|srl|spa|pty|plc|corp|corporation|company|co|kg|ag|aps|kft|sp z o o)\.?$"
)
_PUNCT = re.compile(r"[^\w\s]")
_TRAILING_REQ_ID = re.compile(r"\s*[(\[]?#?\d{4,}[)\]]?\s*$")
_TRAILING_WORKPLACE = re.compile(r"(?i)\s*[-–—(\[]\s*(remote|hybrid|on-?site)\s*[)\]]?\s*$")
_PUNCT_RUNS = re.compile(r"([^\w\s])\1+")
_SEPARATOR_TAIL = re.compile(r"[\s\-–—,;:/|]+$")


def canon(value: str | None) -> str:
    """§4.5.6 verbatim: collapse whitespace, strip, casefold. ``None`` -> ``""``.

    ``None`` and the literal string ``"None"`` must not collide, which is the whole point
    of running every hash input through here instead of ``str()``.
    """
    return " ".join((value or "").split()).strip().casefold()


def canon_locations(locations: list[str] | None) -> str:
    """§4.5.6 verbatim: sorted, deduped, canonicalized, ``|``-joined."""
    return "|".join(sorted({canon(item) for item in (locations or []) if item}))


def fmt_money(value: float | None) -> str:
    """``None`` -> ``""``; a number in one fixed format so ``180000`` and ``180000.0``
    hash identically."""
    return "" if value is None else f"{float(value):.2f}"


def normalize_title(title: str | None, city: str | None = None) -> str | None:
    """``titleNormalized`` (§4.5.6).

    Lower-cases, strips a trailing requisition id, a trailing city that merely repeats the
    parsed location, and a trailing ``(remote)``/``(hybrid)``, then collapses whitespace
    and punctuation runs. Applied repeatedly until stable, because the suffixes appear in
    any order (``"Engineer - Berlin (Remote) #12345"``).
    """
    if not title:
        return None
    text = " ".join(title.split()).casefold()
    city_pattern = None
    if city and city.strip():
        city_pattern = re.compile(
            rf"(?i)\s*[-–—(\[]\s*{re.escape(city.strip().casefold())}\s*[)\]]?\s*$"
        )

    for _ in range(4):  # four strippable suffixes; the loop settles well before that
        before = text
        text = _TRAILING_REQ_ID.sub("", text)
        text = _TRAILING_WORKPLACE.sub("", text)
        if city_pattern:
            text = city_pattern.sub("", text)
        text = _SEPARATOR_TAIL.sub("", text)
        if text == before:
            break

    text = _PUNCT_RUNS.sub(r"\1", text)
    return " ".join(text.split()) or None


def canon_company(name: str | None) -> str:
    """``company_norm`` (§4.5.6): lower-case, strip legal suffixes and punctuation."""
    text = canon(name)
    if not text:
        return ""
    for _ in range(2):  # "Acme Inc. Ltd" happens
        stripped = _LEGAL_SUFFIX.sub("", text)
        if stripped == text:
            break
        text = stripped
    return " ".join(_PUNCT.sub(" ", text).split())


def make_id(provider: str | None, company_slug: str | None, source_id: str | None) -> str:
    """``id = "{provider}:{company_slug}:{source_id}".lower()`` (§4.5.6 verbatim).

    Empty when there is no ``source_id`` to key on. Returning ``"provider:slug:"`` was
    never an empty string, so `dedupe`'s `if record.id:` guard was always true and every
    id-less job on a board shared one key: all but the first were silently counted as
    duplicates and dropped (V1 L10). An id we cannot build is not an id every job shares.
    """
    if not source_id:
        return ""
    return f"{provider or ''}:{company_slug or ''}:{source_id}".lower()


def content_key(
    title_normalized: str | None,
    company: str | None,
    location_raw: str | None,
    requisition_id: str | None = None,
) -> str:
    """16 hex chars over title + company + **raw** location + requisition id (§4.5.6)."""
    payload = "|".join(
        [
            canon(title_normalized),
            canon_company(company),
            canon(location_raw),
            requisition_id or "",
        ]
    )
    return sha1(payload.encode(), usedforsecurity=False).hexdigest()[:16]


def change_hash(
    title: str | None,
    location_strings: list[str] | None,
    department: str | None,
    remote: bool | None,
    employment_type: str | None,
    salary_min: float | None,
    salary_max: float | None,
) -> str:
    """8 hex chars over the fields a buyer would call a change (§4.5.6).

    Excludes the description on purpose: a reworded benefits paragraph is not a change.
    """
    payload = "|".join(
        [
            canon(title),
            canon_locations(location_strings),
            canon(department),
            "" if remote is None else str(remote).lower(),
            employment_type or "",
            fmt_money(salary_min),
            fmt_money(salary_max),
        ]
    )
    return sha1(payload.encode(), usedforsecurity=False).hexdigest()[:8]


def location_strings(locations: list[Location] | None) -> list[str]:
    """The ``changeHash`` view of ``locations[]``: the raw string, or the parsed parts
    when a provider gave structured data and no raw string."""
    values: list[str] = []
    for loc in locations or []:
        values.append(loc.raw or ", ".join(p for p in (loc.city, loc.region, loc.country) if p))
    return [v for v in values if v]


def apply_identity(record: JobRecord) -> JobRecord:
    """Fill ``id``, ``titleNormalized``, ``contentKey`` and ``changeHash`` on a record.

    One call site for all four keys, so the hash inputs cannot drift between the adapter
    path and the history path.
    """
    record.id = make_id(record.provider, record.companySlug, record.sourceId)
    record.titleNormalized = normalize_title(record.title, record.city)
    record.contentKey = content_key(
        record.titleNormalized,
        record.company or record.companySlug,
        record.locationRaw,
        record.requisitionId,
    )
    record.changeHash = change_hash(
        record.title,
        location_strings(record.locations),
        record.department,
        record.remote,
        record.employmentType,
        record.salaryMin,
        record.salaryMax,
    )
    return record


def dedupe_key(record: JobRecord, mode: str = "id") -> str | None:
    """The key a run dedupes on: always ``id``, plus ``contentKey`` when
    ``dedupe: "content"`` (§4.5.6)."""
    return record.contentKey if mode == "content" else record.id
