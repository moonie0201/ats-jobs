"""§4.1 pre-billing filters. Every one of these decides whether a row is charged."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.filters import Filters, parse_posted_after
from core.models import JobRecord, Location

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def job(**kwargs) -> JobRecord:
    base = {"title": "Senior Backend Engineer", "company": "Anthropic"}
    return JobRecord(**{**base, **kwargs})


def test_no_filters_keeps_everything():
    filters = Filters.from_input({})
    assert not filters.active
    assert filters.keep(job())
    assert filters.warnings == []


def test_title_keywords_are_case_insensitive_substrings():
    filters = Filters.from_input({"titleKeywords": ["ENGINEER"]})
    assert filters.keep(job(title="Staff Engineer"))
    assert not filters.keep(job(title="Account Executive"))


def test_exclude_runs_after_include():
    filters = Filters.from_input(
        {"titleKeywords": ["engineer"], "excludeTitleKeywords": ["intern"]}
    )
    assert filters.keep(job(title="Backend Engineer"))
    assert not filters.keep(job(title="Backend Engineer Intern"))


def test_location_keywords_match_raw_and_parsed_parts():
    filters = Filters.from_input({"locationKeywords": ["berlin"]})
    assert filters.keep(job(locationRaw="Berlin, Germany"))
    assert filters.keep(job(city="Berlin"))
    assert not filters.keep(job(locationRaw="Paris, France", city="Paris"))

    code = Filters.from_input({"locationKeywords": ["DE"]})
    assert code.keep(job(countryCode="DE"))


def test_location_keywords_see_secondary_locations():
    record = job(locationRaw="Remote - Americas")
    record.locations = [Location(raw="Berlin, Germany", city="Berlin", countryCode="DE")]
    assert Filters.from_input({"locationKeywords": ["berlin"]}).keep(record)


def test_remote_only_drops_unknown_remote_status():
    filters = Filters.from_input({"remoteOnly": True})
    assert filters.keep(job(remote=True))
    assert not filters.keep(job(remote=False))
    assert not filters.keep(job(remote=None))


def test_departments_match_department_or_team():
    filters = Filters.from_input({"departments": ["engineering"]})
    assert filters.keep(job(department="Engineering"))
    assert filters.keep(job(team="Platform Engineering"))
    assert not filters.keep(job(department="Sales"))


def test_employment_types_keep_unknowns():
    """§4.1: unknown types are kept unless strictEmploymentType is also on."""
    filters = Filters.from_input({"employmentTypes": ["full_time"]})
    assert filters.keep(job(employmentType="full_time", employmentTypeSource="ats"))
    assert not filters.keep(job(employmentType="internship", employmentTypeSource="ats"))
    assert filters.keep(job(employmentType=None))


def test_strict_employment_type_drops_title_guesses():
    """A guess is not confirmation, and this filter decides what gets charged (T-M6)."""
    filters = Filters.from_input({"strictEmploymentType": True})
    assert filters.keep(job(employmentType="contract", employmentTypeSource="ats"))
    assert not filters.keep(job(employmentType="contract", employmentTypeSource="title"))
    assert not filters.keep(job(employmentType=None, employmentTypeSource=None))


def test_strict_and_enum_combine():
    filters = Filters.from_input({"employmentTypes": ["full_time"], "strictEmploymentType": True})
    assert filters.keep(job(employmentType="full_time", employmentTypeSource="ats"))
    assert not filters.keep(job(employmentType="full_time", employmentTypeSource="title"))


def test_posted_after_absolute_date():
    filters = Filters.from_input({"postedAfter": "2026-08-01"}, now=NOW)
    assert filters.keep(job(postedAt="2026-08-19T00:00:00Z"))
    assert not filters.keep(job(postedAt="2026-07-31T23:59:59Z"))


def test_posted_after_keeps_null_dated_jobs():
    """Silently dropping them is the fantastic-jobs complaint we are avoiding (§4.5.5)."""
    filters = Filters.from_input({"postedAfter": "2026-08-01"}, now=NOW)
    assert filters.keep(job(postedAt=None))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7 days", datetime(2026, 8, 19, 12, 0, tzinfo=UTC)),
        ("1 week", datetime(2026, 8, 19, 12, 0, tzinfo=UTC)),
        ("24 hours", datetime(2026, 8, 25, 12, 0, tzinfo=UTC)),
        ("2 days ago", datetime(2026, 8, 24, 12, 0, tzinfo=UTC)),
        ("2026-08-01", datetime(2026, 8, 1, 0, 0, tzinfo=UTC)),
        ("2026-08-01T06:30:00Z", datetime(2026, 8, 1, 6, 30, tzinfo=UTC)),
        (None, None),
        ("", None),
        ("last tuesday", None),
    ],
)
def test_parse_posted_after(value, expected):
    assert parse_posted_after(value, now=NOW) == expected


def test_unreadable_posted_after_warns_instead_of_dropping_rows():
    filters = Filters.from_input({"postedAfter": "last tuesday"}, now=NOW)
    assert filters.warnings == ["invalid_posted_after"]
    assert filters.keep(job(postedAt="2020-01-01T00:00:00Z"))


def test_naive_posted_at_is_treated_as_utc():
    filters = Filters.from_input({"postedAfter": "2026-08-01"}, now=NOW)
    assert filters.keep(job(postedAt="2026-08-19"))
