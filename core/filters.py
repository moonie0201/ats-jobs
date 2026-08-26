"""Pre-billing filters (SPEC v2 §4.1).

Every filter here runs on already-fetched JSON and **before** anything is pushed or
charged, which is what the input schema promises: "Jobs removed by a filter are never
charged, and a company whose jobs were all filtered out costs you nothing at all."
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from core.models import JobRecord

#: `postedAfter` uses Apify's `absoluteOrRelative` datepicker, so the value is either an
#: ISO date/timestamp or a relative phrase like "7 days" (§4.1, §9.3 — no dateutil).
RELATIVE_RE = re.compile(
    r"^\s*(\d+)\s*(minute|hour|day|week|month|year)s?(?:\s+ago)?\s*$", re.IGNORECASE
)
_RELATIVE_UNITS = {
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}


def parse_datetime(value: str | None) -> datetime | None:
    """ISO 8601 (with `Z`) or a bare date -> aware UTC datetime. Junk -> None."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


#: A cutoff older than any ATS posting means the same thing as no cutoff at all, which is
#: why an overflow floors here instead of raising (V3 S21). `RELATIVE_RE` puts no bound on
#: the amount, and there are *two* overflow points, not one: `timedelta * amount` raises
#: on "99999999999999999999 years", and "10000 years" gets past the multiply only to
#: overflow at the subtraction below — so clamping the amount does not cover it and one
#: `except OverflowError` around both does.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def parse_posted_after(value: str | None, *, now: datetime | None = None) -> datetime | None:
    """ "2026-08-01" or "7 days" -> cutoff. Unparseable -> None (caller warns).

    Never raises. `Filters.from_input` is called from `src/main.py` inside `async with
    Actor:` but outside every `try`, before a single company is resolved, so an exception
    here exits the run FAILED with a raw traceback — no error row, no summary, nothing the
    buyer can act on. That is the opposite of the shell's "degrade, never fail" posture.
    """
    if not value or not str(value).strip():
        return None
    text = str(value).strip()
    match = RELATIVE_RE.match(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        try:
            return (now or datetime.now(UTC)) - _RELATIVE_UNITS[unit] * amount
        except OverflowError:
            return _EPOCH
    return parse_datetime(text)


def _folded(values: Iterable[Any] | None) -> tuple[str, ...]:
    if not values:
        return ()
    return tuple(str(v).strip().casefold() for v in values if str(v).strip())


def _contains_any(haystacks: Iterable[str | None], needles: Sequence[str]) -> bool:
    folded = [h.casefold() for h in haystacks if h]
    return any(needle in hay for hay in folded for needle in needles)


@dataclass(slots=True)
class Filters:
    """The seven pre-billing filters, built once per run from the Actor input."""

    title_keywords: tuple[str, ...] = ()
    exclude_title_keywords: tuple[str, ...] = ()
    location_keywords: tuple[str, ...] = ()
    remote_only: bool = False
    departments: tuple[str, ...] = ()
    employment_types: frozenset[str] = frozenset()
    strict_employment_type: bool = False
    posted_after: datetime | None = None
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_input(cls, data: dict[str, Any], *, now: datetime | None = None) -> Filters:
        warnings: list[str] = []
        raw_posted_after = data.get("postedAfter")
        posted_after = parse_posted_after(raw_posted_after, now=now)
        if raw_posted_after and posted_after is None:
            # Never drop rows on a filter we could not read; say so instead.
            warnings.append("invalid_posted_after")
        return cls(
            title_keywords=_folded(data.get("titleKeywords")),
            exclude_title_keywords=_folded(data.get("excludeTitleKeywords")),
            location_keywords=_folded(data.get("locationKeywords")),
            remote_only=bool(data.get("remoteOnly", False)),
            departments=_folded(data.get("departments")),
            employment_types=frozenset(_folded(data.get("employmentTypes"))),
            strict_employment_type=bool(data.get("strictEmploymentType", False)),
            posted_after=posted_after,
            warnings=warnings,
        )

    @property
    def active(self) -> bool:
        return bool(
            self.title_keywords
            or self.exclude_title_keywords
            or self.location_keywords
            or self.remote_only
            or self.departments
            or self.employment_types
            or self.strict_employment_type
            or self.posted_after
        )

    def _location_matches(self, record: JobRecord) -> bool:
        """`locationKeywords`, with a two-letter needle read as a country **code**.

        A plain substring test over the whole haystack kept `"Toulouse, France"` for
        `["US"]` and `"Stockholm, Sweden"` for `["DE"]` — and the input schema recommended
        `DE` verbatim as its example, so the schema induced its own worst case. This filter
        runs *before* billing, which makes every false positive a charged row the buyer did
        not want: the §8.2 failure mode arriving through a different door (V1 M2).

        Short needles are matched exactly against `countryCode` only; anything longer keeps
        the substring behaviour, so `"Berlin"`, `"Germany"` and `"EMEA"` work as before.
        """
        codes = {
            code.casefold()
            for code in (record.countryCode, *(loc.countryCode for loc in record.locations))
            if code
        }
        short = {n for n in self.location_keywords if len(n) <= 2}
        if codes & short:
            return True
        long_ = [n for n in self.location_keywords if len(n) > 2]
        if not long_:
            return False
        haystacks: list[str | None] = [
            record.locationRaw,
            record.city,
            record.region,
            record.country,
            record.countryCode,
        ]
        for loc in record.locations:
            haystacks += [loc.raw, loc.city, loc.region, loc.country, loc.countryCode]
        return _contains_any(haystacks, long_)

    def keep(self, record: JobRecord) -> bool:
        """True when the job survives every filter. Order follows §4.1's descriptions."""
        title = record.title or ""
        if self.title_keywords and not _contains_any([title], self.title_keywords):
            return False
        if self.exclude_title_keywords and _contains_any([title], self.exclude_title_keywords):
            return False

        if self.location_keywords and not self._location_matches(record):
            return False

        # "Jobs with unknown remote status are dropped" (§4.1) — remote is a tri-state.
        if self.remote_only and record.remote is not True:
            return False

        if self.departments and not _contains_any(
            [record.department, record.team], self.departments
        ):
            return False

        # strictEmploymentType = ATS-confirmed only; a title guess is not confirmation
        # (§4.1, V2 T-M6).
        if self.strict_employment_type and record.employmentTypeSource != "ats":
            return False
        if self.employment_types:
            kind = record.employmentType
            if kind is not None and kind.casefold() not in self.employment_types:
                return False
            if kind is None and self.strict_employment_type:
                return False

        if self.posted_after is not None:
            posted = parse_datetime(record.postedAt)
            # Null-dated jobs are kept on purpose and the README says so (§4.5.5).
            if posted is not None and posted < self.posted_after:
                return False

        return True
