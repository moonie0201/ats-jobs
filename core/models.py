"""Data models shared by every adapter, normalizer and Actor (SPEC v2 §4.2, §4.6, §5, §9.1).

Plain stdlib dataclasses, not pydantic: the dataset schema (§4.2) is deliberately loose
(no ``required``, every field nullable) and validation happens on Apify's side at push
time, so a second validation layer here would only add a dependency and a place for the
two definitions to drift apart.

Field names on :class:`JobRecord` are the dataset-schema keys verbatim, so
:meth:`JobRecord.to_item` is a dict build plus a profile filter, with no rename table.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

# --- vocabularies (kept identical to .actor/input_schema.json + dataset_schema.json) ---

PROVIDERS: tuple[str, ...] = (
    "greenhouse",
    "lever",
    "ashby",
    "recruitee",
    "rippling",
    "personio",
)

EMPLOYMENT_TYPES: tuple[str, ...] = (
    "full_time",
    "part_time",
    "contract",
    "temporary",
    "internship",
    "other",
)

WORKPLACE_TYPES: tuple[str, ...] = ("onsite", "hybrid", "remote")
SALARY_INTERVALS: tuple[str, ...] = ("year", "month", "week", "day", "hour")

#: ``status`` values on ``company_summary`` and ``error`` rows (§4.2, §5.12).
STATUSES: tuple[str, ...] = (
    "ok",
    "not_found",
    "unconfirmed",
    "rate_limited",
    "http_error",
    "parse_error",
    "timeout",
    "max_jobs_reached",
    "budget_exhausted",
    "no_companies",
    "unresolved_domain",
)

# --- output profiles (§4.6) ---

MINIMAL_FIELDS: tuple[str, ...] = (
    "recordType",
    "id",
    "title",
    "company",
    "locationRaw",
    "city",
    "countryCode",
    "remote",
    "url",
    "postedAt",
)

COMPACT_FIELDS: tuple[str, ...] = MINIMAL_FIELDS + (
    "provider",
    "companySlug",
    "department",
    "employmentType",
    "employmentTypeSource",
    "salaryMin",
    "salaryMax",
    "salaryCurrency",
    "salaryInterval",
    "applyUrl",
    "updatedAt",
    "isNew",
)

#: ``None`` means "every field" — the ``full`` profile.
PROFILE_FIELDS: dict[str, tuple[str, ...] | None] = {
    "minimal": MINIMAL_FIELDS,
    "compact": COMPACT_FIELDS,
    "full": None,
}


@dataclass(slots=True)
class Ref:
    """A resolved company: which provider to ask, and which board to ask for (§5, §5.11).

    ``slug`` keeps the casing the directory validated, because Lever site names are
    case-sensitive (§5.11). Never rebuild a fetch URL from a lower-cased job ``id``.
    """

    provider: str
    slug: str
    site: str | None = None
    region: str | None = None
    #: The exact entry from the user's ``companies`` list that produced this Ref.
    #: Carried here so every emitted row can fill the dataset's ``input`` field (§4.2)
    #: without a parallel lookup table.
    input: str | None = None


@dataclass(slots=True)
class Meta:
    """Per-company metadata returned alongside the raw job list (§5)."""

    company_name: str | None = None
    total: int | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Location:
    """One parsed location. Field names match the ``locations[]`` item keys in §4.6."""

    raw: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    countryCode: str | None = None

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        """Ordering for ``locations[]`` (§4.5.1 step 8) so a provider reshuffle cannot
        flip ``changeHash`` and emit a phantom ``loc`` change."""
        return (self.countryCode or "", self.region or "", self.city or "", self.raw or "")


@dataclass(slots=True)
class Salary:
    """A parsed pay range (§4.5.3). ``source`` is ``ats``, ``parsed`` or ``None``."""

    min: float | None = None
    max: float | None = None
    currency: str | None = None
    interval: str | None = None
    source: str | None = None
    raw: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """The policy half of the adapter contract (§5), as data rather than as memory.

    ``ai_train=False`` (Lever) blocks derivative-dataset / fine-tuning use at the type
    level; ``retainable=False`` keeps a provider's companies out of the §7 history store.
    """

    name: str
    host_rate_limit: float = 2.0
    needs_detail_call: bool = False
    ai_train: bool = True
    retainable: bool = True


@dataclass(slots=True)
class JobRecord:
    """One ``job`` dataset row (§4.2 fields, §4.6 example).

    Everything defaults to ``None`` because "we could not determine it" is the honest
    and common case; nothing here is ever guessed into a value.
    """

    # Field names are the dataset-schema keys verbatim (camelCase), never snake_case.
    recordType: str = "job"
    id: str | None = None
    contentKey: str | None = None
    changeHash: str | None = None
    provider: str | None = None
    companySlug: str | None = None
    company: str | None = None
    companyDomain: str | None = None
    title: str | None = None
    titleNormalized: str | None = None
    department: str | None = None
    team: str | None = None
    locationRaw: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    countryCode: str | None = None
    locations: list[Location] = field(default_factory=list)
    remote: bool | None = None
    workplaceType: str | None = None
    remoteSource: str | None = None
    employmentType: str | None = None
    employmentTypeRaw: str | None = None
    employmentTypeSource: str | None = None
    seniority: str | None = None
    yearsOfExperience: str | None = None
    salaryMin: float | None = None
    salaryMax: float | None = None
    salaryCurrency: str | None = None
    salaryInterval: str | None = None
    salarySource: str | None = None
    salaryRaw: str | None = None
    url: str | None = None
    applyUrl: str | None = None
    postedAt: str | None = None
    postedAtSource: str | None = None
    updatedAt: str | None = None
    descriptionHtml: str | None = None
    descriptionText: str | None = None
    descriptionRedacted: bool | None = None
    requisitionId: str | None = None
    sourceId: str | None = None
    dedupedFrom: list[str] | None = None
    isNew: bool | None = None
    firstSeenAt: str | None = None
    scrapedAt: str | None = None
    raw: dict[str, Any] | None = None
    input: str | None = None
    #: Per-job warnings, e.g. ``["detail_failed"]`` when the detail call failed but the
    #: job was still delivered (§5.12).
    warnings: list[str] = field(default_factory=list)

    def apply_location(
        self, primary: Location, all_locations: list[Location] | None = None
    ) -> None:
        """Flatten a parsed :class:`Location` onto the primary location fields.

        ``all_locations`` fills ``locations[]``; it is sorted here so callers cannot
        forget §4.5.1 step 8. Defaults to ``[primary]``.
        """
        self.locationRaw = primary.raw
        self.city = primary.city
        self.region = primary.region
        self.country = primary.country
        self.countryCode = primary.countryCode
        locs = list(all_locations) if all_locations is not None else [primary]
        self.locations = sorted(locs, key=lambda loc: loc.sort_key)

    def apply_salary(self, salary: Salary) -> None:
        """Flatten a parsed :class:`Salary` onto the ``salary*`` fields."""
        self.salaryMin = salary.min
        self.salaryMax = salary.max
        self.salaryCurrency = salary.currency
        self.salaryInterval = salary.interval
        self.salarySource = salary.source
        self.salaryRaw = salary.raw

    def to_item(self, profile: str = "full") -> dict[str, Any]:
        """Render the dataset item for ``outputProfile`` (§4.6).

        Unknown profile names fall back to ``full`` rather than raising: dropping fields
        the buyer paid for is worse than emitting extra ones.
        """
        item: dict[str, Any] = {f.name: getattr(self, f.name) for f in fields(self)}
        item["locations"] = [asdict(loc) for loc in self.locations] or None
        item["dedupedFrom"] = self.dedupedFrom or None
        item["warnings"] = self.warnings or None
        keep = PROFILE_FIELDS.get(profile, None)
        if keep is None:
            return item
        return {name: item[name] for name in keep}


def demo() -> None:
    """Self-check: the §4.6 example record round-trips and the profiles slice it."""
    rec = JobRecord(
        id="greenhouse:anthropic:4019283",
        provider="greenhouse",
        companySlug="anthropic",
        company="Anthropic",
        title="Senior Backend Engineer, Inference",
        url="https://job-boards.greenhouse.io/anthropic/jobs/4019283",
        postedAt="2026-08-19T00:00:00Z",
    )
    rec.apply_location(
        Location("San Francisco, CA", "San Francisco", "CA", "United States", "US"),
        [
            Location("Remote - EMEA", None, None, None, None),
            Location("San Francisco, CA", "San Francisco", "CA", "United States", "US"),
        ],
    )
    rec.apply_salary(Salary(300000, 405000, "USD", "year", "ats", None))

    full = rec.to_item()
    assert len(full) == len(fields(JobRecord)) == 49, len(full)
    assert full["salaryMin"] == 300000 and full["salaryInterval"] == "year"
    assert full["city"] == "San Francisco" and full["countryCode"] == "US"
    assert full["locations"][0]["raw"] == "Remote - EMEA", "sorted by (cc, region, city, raw)"
    assert full["remote"] is None and full["employmentType"] is None, "never guessed"
    assert full["warnings"] is None and full["dedupedFrom"] is None

    assert set(rec.to_item("minimal")) == set(MINIMAL_FIELDS)
    assert set(rec.to_item("compact")) == set(COMPACT_FIELDS)
    assert set(rec.to_item("nonsense")) == set(full), "unknown profile falls back to full"

    empty = JobRecord().to_item()
    assert empty["recordType"] == "job" and empty["locations"] is None
    print("core.models demo OK")


if __name__ == "__main__":
    demo()
