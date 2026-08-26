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


# --- V1 M2: a two-letter keyword is a country code, not a substring -------------------


@pytest.mark.parametrize(
    ("keyword", "location", "code"),
    [("US", "Toulouse, France", "FR"), ("DE", "Stockholm, Sweden", "SE")],
)
def test_a_two_letter_keyword_does_not_match_inside_a_word(keyword, location, code):
    """V1 M2: `US` matched inside `touloUSe` and `DE` inside `sweDEn`, and the schema
    recommended `DE` verbatim as its own example. This filter runs *before* billing, so
    every false positive was a charged row the buyer did not ask for."""
    filters = Filters.from_input({"locationKeywords": [keyword]})
    assert not filters.keep(job(locationRaw=location, city=location, countryCode=code))


def test_a_two_letter_keyword_still_matches_the_country_code():
    filters = Filters.from_input({"locationKeywords": ["DE"]})
    assert filters.keep(job(locationRaw="Berlin", city="Berlin", countryCode="DE"))
    assert filters.keep(
        job(locationRaw="Remote", locations=[Location(raw="Munich", countryCode="DE")])
    )


def test_longer_keywords_keep_their_substring_behaviour():
    filters = Filters.from_input({"locationKeywords": ["Berlin", "EMEA"]})
    assert filters.keep(job(locationRaw="Berlin, Germany"))
    assert filters.keep(job(locationRaw="EMEA - remote"))
    assert not filters.keep(job(locationRaw="Toulouse, France"))


# --- V3 S21: an unbounded relative cutoff used to fail the whole run -------------------


@pytest.mark.parametrize(
    "value", ["99999999999999999999 years", "10000 years", "1000000000 days", "10000000000 days"]
)
def test_a_huge_relative_posted_after_does_not_overflow(value: str):
    """V3 S21: `timedelta * amount` and `now - delta` are two separate overflow points, so
    clamping the amount does not cover it. `Filters.from_input` runs outside every `try` in
    `src/main.py`, before a company is resolved — an exception there exits the run FAILED
    with a raw traceback and no error row at all."""
    cutoff = parse_posted_after(value, now=NOW)
    assert cutoff is not None
    assert cutoff.year == 1970, "a cutoff older than any posting is the same as no cutoff"
    assert Filters.from_input({"postedAfter": value}).posted_after is not None


def test_ordinary_relative_cutoffs_are_unchanged():
    assert parse_posted_after("7 days", now=NOW) == datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    assert parse_posted_after("1 month", now=NOW) == datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
