"""``build_job_record()`` — the shared adapter path, end to end (§4.5, §4.6)."""

from __future__ import annotations

import pytest

from core.models import Ref
from core.normalize.record import build_job_record

REF = Ref("greenhouse", "anthropic", input="https://job-boards.greenhouse.io/anthropic")

# The §4.6 example job, as a Greenhouse adapter would hand it over.
ANTHROPIC = {
    "job": {
        "pay_input_ranges": [
            {
                "min_cents": 30000000,
                "max_cents": 40500000,
                "currency_type": "USD",
                "title": "Annual Salary Range",
            }
        ]
    },
    "sourceId": "4019283",
    "title": "Senior Backend Engineer, Inference",
    "company": "Anthropic",
    "companyDomain": "anthropic.com",
    "department": "Engineering",
    "locationRaw": "San Francisco, CA",
    "url": "https://job-boards.greenhouse.io/anthropic/jobs/4019283",
    "applyUrl": "https://job-boards.greenhouse.io/anthropic/jobs/4019283",
    "postedAt": "2026-08-19",
    "postedAtSource": "first_published",
    "updatedAt": "2026-08-22T11:04:00+00:00",
    "requisitionId": "REQ-4821",
    "descriptionHtml": "<p>About Anthropic… Questions? Reach out to jobs@anthropic.com.</p>",
}

OPTIONS = {
    "includeDescription": True,
    "descriptionFormat": "text",
    "scrapedAt": "2026-08-26T03:00:12Z",
}


@pytest.fixture
def row():
    return build_job_record(REF, ANTHROPIC, OPTIONS)


def test_reproduces_the_spec_example(row):
    item = row.to_item()
    expected = {
        "recordType": "job",
        "id": "greenhouse:anthropic:4019283",
        "provider": "greenhouse",
        "companySlug": "anthropic",
        "company": "Anthropic",
        "companyDomain": "anthropic.com",
        "title": "Senior Backend Engineer, Inference",
        "titleNormalized": "senior backend engineer, inference",
        "department": "Engineering",
        "team": None,
        "locationRaw": "San Francisco, CA",
        "city": "San Francisco",
        "region": "CA",
        "country": "United States",
        "countryCode": "US",
        "remote": None,
        "workplaceType": None,
        "remoteSource": None,
        "employmentType": None,
        "employmentTypeRaw": None,
        "employmentTypeSource": None,
        "seniority": None,
        "yearsOfExperience": None,
        "salaryMin": 300000,
        "salaryMax": 405000,
        "salaryCurrency": "USD",
        "salaryInterval": "year",
        "salarySource": "ats",
        "salaryRaw": None,
        "url": "https://job-boards.greenhouse.io/anthropic/jobs/4019283",
        "applyUrl": "https://job-boards.greenhouse.io/anthropic/jobs/4019283",
        "postedAt": "2026-08-19T00:00:00Z",
        "postedAtSource": "first_published",
        "updatedAt": "2026-08-22T11:04:00Z",
        "descriptionHtml": None,
        "descriptionText": "About Anthropic… Questions? Reach out to [redacted].",
        "descriptionRedacted": True,
        "requisitionId": "REQ-4821",
        "sourceId": "4019283",
        "dedupedFrom": None,
        "raw": None,
        "input": "https://job-boards.greenhouse.io/anthropic",
    }
    assert {k: item[k] for k in expected} == expected
    assert item["locations"] == [
        {
            "raw": "San Francisco, CA",
            "city": "San Francisco",
            "region": "CA",
            "country": "United States",
            "countryCode": "US",
        }
    ]
    assert item["scrapedAt"] == "2026-08-26T03:00:12Z"
    assert len(item["contentKey"]) == 16 and len(item["changeHash"]) == 8


def test_nothing_is_guessed(row):
    # Greenhouse reports no remote flag, no team and no employment type, and neither the
    # title nor the location said otherwise.
    assert (row.remote, row.team, row.employmentType) == (None, None, None)


@pytest.mark.parametrize(
    ("fmt", "html_out", "text_out"),
    [("text", False, True), ("html", True, False), ("both", True, True)],
)
def test_description_format(fmt, html_out, text_out):
    row = build_job_record(REF, ANTHROPIC, OPTIONS | {"descriptionFormat": fmt})
    assert (row.descriptionHtml is not None) is html_out
    assert (row.descriptionText is not None) is text_out


def test_description_is_dropped_but_still_feeds_salary_parsing():
    extracted = ANTHROPIC | {
        "job": {},
        "descriptionHtml": "<p>The base pay is $180,000 - $240,000 per year.</p>",
    }
    row = build_job_record(REF, extracted, {"includeDescription": False})
    assert row.descriptionText is None and row.descriptionHtml is None
    assert row.descriptionRedacted is None
    assert (row.salaryMin, row.salarySource) == (180000, "parsed")


def test_remote_rank_five_stays_opt_in():
    extracted = ANTHROPIC | {"descriptionHtml": "<p>This is a fully remote role.</p>"}
    assert build_job_record(REF, extracted, {"includeDescription": False}).remote is None
    assert build_job_record(REF, extracted, {"includeDescription": True}).remoteSource == (
        "description"
    )


def test_raw_payload_only_when_asked():
    assert build_job_record(REF, ANTHROPIC, OPTIONS).raw is None
    assert build_job_record(REF, ANTHROPIC, OPTIONS | {"includeRawJson": True}).raw is not None


def test_locations_are_flattened_and_sorted():
    extracted = ANTHROPIC | {
        "locationRaw": "San Francisco, CA",
        "locations": ["New York, NY", "Remote - EMEA"],
    }
    row = build_job_record(REF, extracted, OPTIONS)
    assert row.city == "San Francisco"
    assert [loc.raw for loc in row.locations] == [
        "Remote - EMEA",
        "San Francisco, CA",
        "New York, NY",
    ]


def test_warnings_ride_along():
    row = build_job_record(REF, ANTHROPIC | {"warnings": ["detail_failed"]}, OPTIONS)
    assert row.to_item()["warnings"] == ["detail_failed"]


def test_output_profiles_slice_the_row(row):
    assert set(row.to_item("minimal")) < set(row.to_item("compact")) < set(row.to_item("full"))


def test_a_job_with_almost_nothing_in_it_does_not_raise():
    row = build_job_record(Ref("lever", "acme"), {"sourceId": "x", "title": None})
    assert row.id == "lever:acme:x"
    assert row.title is None and row.titleNormalized is None
    assert row.locations == [] and row.city is None
    assert row.salarySource is None and row.remote is None
    assert row.scrapedAt.endswith("Z")


def test_every_nested_provider_object_empty():
    """The §10.1 ``test_adapters_empty_objects`` shape, for the shared path."""
    extracted = {
        "job": {
            "compensation": {},
            "salaryRange": {},
            "salary": {},
            "pay_input_ranges": [],
            "payRangeDetails": [],
            "workplaceType": None,
        },
        "sourceId": "1",
        "title": "Engineer",
        "locationRaw": "",
        "locations": [],
        "locationStructured": {},
        "employmentType": {},
        "postedAt": "",
        "descriptionHtml": "",
    }
    row = build_job_record(Ref("ashby", "acme"), extracted, OPTIONS)
    assert row.employmentType is None and row.salaryMin is None
    assert row.postedAt is None and row.postedAtSource is None
    assert row.locations == []


def test_provider_fields_flow_through():
    extracted = {
        "job": {"workplaceType": "Remote"},
        "sourceId": "77",
        "title": "Senior Consultant",
        "seniority": "experienced",
        "yearsOfExperience": "7-10",
        "employmentType": "permanent",
        "schedule": "part-time",
        "locationRaw": "München",
        "postedAt": "2024-11-13T14:10:41+00:00",
        "postedAtSource": "createdAt",
    }
    row = build_job_record(Ref("personio", "acme"), extracted, OPTIONS)
    assert (row.seniority, row.yearsOfExperience) == ("experienced", "7-10")
    assert (row.employmentType, row.employmentTypeSource) == ("part_time", "ats")
    assert (row.remote, row.remoteSource) == (True, "ats")
    assert row.postedAt == "2024-11-13T14:10:41Z"
    assert row.postedAtSource == "createdAt"
