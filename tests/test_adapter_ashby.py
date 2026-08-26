"""Ashby adapter (SPEC v2 §5.3, §10.1). Offline: respx serves every HTTP call.

`tests/fixtures/ashby/openai.json` and `ramp.json` are real payloads captured from
`api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` on 2026-08-26,
truncated to the first 16 / 14 postings by `scripts/refresh_fixtures.py --limit`.
`tests/fixtures/_empty_objects/ashby.json` is hand-built: no live board produces it.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.http import Client, NotFound, ParseError, make_client
from core.models import JobRecord, Ref
from core.providers import get_adapter
from core.providers.ashby import LIST_URL, SPEC, fetch, list_jobs, to_record
from tests.conftest import FIXTURES_DIR, load_fixture

OPENAI = Ref("ashby", "openai", input="ashby:openai")
RAMP = Ref("ashby", "ramp", input="https://jobs.ashbyhq.com/ramp")

#: `includeDescription` is on so rank 5 of §4.5.2 and the redaction path are exercised;
#: `scrapedAt` is pinned so a record is byte-stable across a run.
OPTIONS: dict[str, Any] = {
    "includeDescription": True,
    "descriptionFormat": "both",
    "scrapedAt": "2026-08-26T00:00:00Z",
}


@pytest.fixture
def client() -> Client:
    """Instant backoffs and an instant rate cap — no test waits on the token bucket."""
    clock = [0.0]

    async def fake_sleep(delay: float) -> None:
        clock[0] += delay

    return make_client(timeout_secs=5, sleep=fake_sleep, clock=lambda: clock[0])


def board(slug: str) -> dict[str, Any]:
    return load_fixture("ashby", f"{slug}.json")


def records(slug: str, ref: Ref) -> list[JobRecord]:
    return [to_record(job, ref, OPTIONS) for job in board(slug)["jobs"]]


# --- §10.1 exact values for the first record -------------------------------------------


def test_openai_first_record_exact_values():
    row = records("openai", OPENAI)[0]

    assert row.recordType == "job"
    assert row.id == "ashby:openai:8fb1615c-34bf-47c4-a1d1-b7b2f836bbd3"
    assert row.sourceId == "8fb1615c-34bf-47c4-a1d1-b7b2f836bbd3"
    assert row.provider == "ashby"
    assert row.companySlug == "openai"
    assert row.title == "Technical Program Manager, Compute Infrastructure"
    assert row.department == "Technical Program Management"
    assert row.team == "Technical Program Management"

    # §4.5.1 step 1: `address.postalAddress` wins outright, and the code is upper-case.
    assert row.locationRaw == "San Francisco"
    assert row.city == "San Francisco"
    assert row.region == "California"
    assert row.country == "United States"
    assert row.countryCode == "US"

    # `isRemote` and `workplaceType` are both null on this posting and "San Francisco"
    # carries no marker — §4.5.2 rank 6, not a guess.
    assert row.remote is None
    assert row.workplaceType is None
    assert row.remoteSource is None

    assert row.employmentType == "full_time"
    assert row.employmentTypeRaw == "FullTime"
    assert row.employmentTypeSource == "ats"

    assert row.salaryMin == 257000
    assert row.salaryMax == 335000
    assert row.salaryCurrency == "USD"
    assert row.salaryInterval == "year"
    assert row.salarySource == "ats"
    assert row.salaryRaw == "$257K – $335K • Offers Equity"

    assert row.postedAt == "2026-03-12T16:38:15Z"
    assert row.postedAtSource == "publishedAt"
    assert row.url == "https://jobs.ashbyhq.com/openai/8fb1615c-34bf-47c4-a1d1-b7b2f836bbd3"
    assert row.applyUrl == (
        "https://jobs.ashbyhq.com/openai/8fb1615c-34bf-47c4-a1d1-b7b2f836bbd3/application"
    )
    assert row.input == "ashby:openai"


def test_ramp_first_record_exact_values():
    row = records("ramp", RAMP)[0]

    assert row.id == "ashby:ramp:34413f8d-26bf-4bbc-8ade-eb309a0e2245"
    # The board's own value is " Security Engineer, Cloud" — leading space included.
    assert row.title == "Security Engineer, Cloud"
    assert row.department == "Engineering"
    assert row.team == "Backend"

    # Structured parts beat the free text: `location` reads "New York, NY (HQ)" while
    # `postalAddress` says New York City / NY / USA. `raw` keeps what the board published.
    assert row.locationRaw == "New York, NY (HQ)"
    assert row.city == "New York City"
    assert row.region == "NY"
    assert row.countryCode == "US"

    assert (row.remote, row.workplaceType, row.remoteSource) == (False, "hybrid", "ats")
    assert row.employmentType == "full_time"
    assert row.employmentTypeSource == "ats"
    assert row.salaryMin == 211400
    assert row.salaryMax == 290600
    assert row.salaryCurrency == "USD"
    assert row.salaryInterval == "year"
    assert row.postedAt == "2026-04-07T17:12:35Z"
    assert row.url == "https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245"


# --- provider-specific edge cases (§10.1, §5.3) ----------------------------------------


def test_one_year_interval_keeps_the_money(fixture):
    """§10.1: Ashby `"1 YEAR"` → `salaryInterval == "year"` with min/max/currency all
    preserved. v1's `PER_YEAR` map would have nulled the interval and, by §4.5.3 step 2,
    dropped the whole band on the provider with the cleanest structured pay of the six."""
    checked = 0
    for slug, ref in (("openai", OPENAI), ("ramp", RAMP)):
        for job in fixture("ashby", f"{slug}.json")["jobs"]:
            components = [
                component
                for component in (job.get("compensation") or {}).get("summaryComponents") or []
                if component.get("compensationType") == "Salary"
                and component.get("interval") == "1 YEAR"
            ]
            if not components:
                continue
            row = to_record(job, ref, OPTIONS)
            assert row.salaryInterval == "year", row.title
            assert row.salaryMin == min(c["minValue"] for c in components)
            assert row.salaryMax == max(c["maxValue"] for c in components)
            assert row.salaryCurrency == components[0]["currencyCode"]
            assert row.salarySource == "ats"
            checked += 1
    assert checked >= 20  # the fixtures really do carry structured pay


def test_compensation_is_requested_on_the_list_call():
    """Without `includeCompensation=true` every `salary*` field above is null (§5.3)."""
    from core.providers.ashby import LIST_PARAMS

    assert LIST_PARAMS == {"includeCompensation": "true"}


def test_ats_flags_drive_remote_and_hybrid_never_reads_true():
    """§4.5.2 rank 1. `isRemote: true` with `workplaceType: "Hybrid"` occurs on 450 of the
    751 live openai postings; a hybrid role must never be sold as remote."""
    rows = {row.title: row for row in records("openai", OPENAI)}

    hybrid = rows["Software Engineer, RL Training Infra"]
    assert (hybrid.remote, hybrid.workplaceType, hybrid.remoteSource) == (False, "hybrid", "ats")

    remote = rows["Clean Energy and New Technology Lead"]
    assert (remote.remote, remote.workplaceType, remote.remoteSource) == (True, "remote", "ats")
    # §4.5.1 step 6: "US - Remote" names no city, and the country still resolves.
    assert (remote.city, remote.region, remote.countryCode) == (None, None, "US")


def test_secondary_locations_are_merged_structured_and_sorted():
    """`locations[] = [location] + secondaryLocations[].location`, each with its own
    `address.postalAddress`, sorted by §4.5.1 step 8 so a reshuffle cannot flip
    `changeHash`."""
    row = next(
        r for r in records("openai", OPENAI) if r.title == "Software Engineer, Data Infrastructure"
    )

    assert [loc.raw for loc in row.locations] == [
        "Mountain View",
        "San Francisco",
        "New York City",
        "Seattle",
    ]
    assert [loc.region for loc in row.locations] == [
        "California",
        "California",
        "New York",
        "Washington",
    ]
    assert row.locations == sorted(row.locations, key=lambda loc: loc.sort_key)
    # The primary stays the job's own `location`, not the sorted head.
    assert row.locationRaw == "San Francisco"


def test_secondary_location_structured_parts_stay_aligned():
    """Ramp's first posting is NY + Remote (Canada) + Remote (US) + Miami: each country
    must come off its *own* `postalAddress`, never the primary's."""
    row = records("ramp", RAMP)[0]
    by_raw = {loc.raw: loc for loc in row.locations}

    assert by_raw["Remote (Canada)"].countryCode == "CA"
    assert by_raw["Remote (US)"].countryCode == "US"
    assert by_raw["Miami, FL"].city == "Miami"
    assert by_raw["Miami, FL"].region == "Florida"
    assert by_raw["New York, NY (HQ)"].city == "New York City"


def test_description_comes_from_the_list_call_no_detail_request():
    """§5.3: `needs_detail_call` is False — `descriptionHtml`/`descriptionPlain` are inline."""
    assert SPEC.needs_detail_call is False
    row = records("openai", OPENAI)[0]
    # The fixture's prose is scrubbed (`scripts/scrub_fixtures.py`), so this asserts that a
    # body arrived from the *list* payload at all, not what it says.
    assert row.descriptionText and "<" not in row.descriptionText
    assert row.descriptionHtml and row.descriptionHtml.startswith("<h3>")
    assert row.descriptionRedacted is False


def test_spec_matches_the_contract():
    assert (SPEC.name, SPEC.host_rate_limit) == ("ashby", 2.0)
    assert SPEC.ai_train is True and SPEC.retainable is True
    assert get_adapter("ashby").to_record is to_record


# --- §10.1 `test_adapters_empty_objects` -----------------------------------------------


def test_empty_objects_never_raise_and_never_guess():
    """Every nested Ashby object (`address`, `compensation`, `employmentType`,
    `secondaryLocations`) is `{}`, `null`, blank or absent. No exception, all-null output.
    This is the test that catches the day Ashby ships a schema change."""
    payload = json.loads((FIXTURES_DIR / "_empty_objects" / "ashby.json").read_text("utf-8"))
    rows = [to_record(job, Ref("ashby", "acme"), OPTIONS) for job in payload["jobs"]]

    assert [row.id for row in rows] == [
        "ashby:acme:empty-nulls",
        "ashby:acme:empty-absent",
        "ashby:acme:empty-nested",
    ]
    for row in rows:
        assert row.title  # the only two fields the payload keeps
        assert row.locations == []
        for field in (
            "department",
            "team",
            "locationRaw",
            "city",
            "region",
            "country",
            "countryCode",
            "remote",
            "workplaceType",
            "remoteSource",
            "employmentType",
            "employmentTypeRaw",
            "employmentTypeSource",
            "salaryMin",
            "salaryMax",
            "salaryCurrency",
            "salaryInterval",
            "salarySource",
            "salaryRaw",
            "postedAt",
            "postedAtSource",
            "url",
            "applyUrl",
            "descriptionHtml",
            "descriptionText",
        ):
            assert getattr(row, field) is None, f"{row.id}.{field}"


# --- transport: pagination, the isListed guard and §5.12 failures ----------------------


def openai_route(**kwargs: Any) -> respx.Route:
    return respx.get(LIST_URL.format(slug="openai"), **kwargs)


@respx.mock
async def test_fetch_reads_one_page_with_compensation(client: Client):
    """§5.3: no pagination — one GET, every posting."""
    payload = board("openai")
    route = openai_route().mock(return_value=httpx.Response(200, json=payload))

    rows = await fetch(OPENAI, client, OPTIONS)

    assert route.call_count == 1
    assert len(rows) == len(payload["jobs"]) == 16
    assert all(isinstance(row, JobRecord) for row in rows)
    request = route.calls[0].request
    assert request.url.params["includeCompensation"] == "true"
    assert request.url.path == "/posting-api/job-board/openai"


@respx.mock
async def test_slug_casing_is_preserved_in_the_url(client: Client):
    """`openai`/`OpenAI`/`OPENAI` all 200 server-side (§5.3), but the Ref's casing is what
    the directory validated (§6.4) and the adapter must not rewrite it."""
    route = respx.get(LIST_URL.format(slug="OpenAI")).mock(
        return_value=httpx.Response(200, json={"jobs": [], "apiVersion": "1"})
    )
    assert await fetch(Ref("ashby", "OpenAI"), client) == []
    assert route.calls[0].request.url.path == "/posting-api/job-board/OpenAI"


@respx.mock
async def test_jobs_without_the_isListed_flag_are_kept(client: Client):
    """The `isListed == false` drop is defensive (measured 0 of 754 on openai, V2 T-M8):
    a board that stops sending the flag must not come back empty."""
    payload = {"jobs": [{"id": "a", "title": "Kept"}], "apiVersion": "1"}
    openai_route().mock(return_value=httpx.Response(200, json=payload))

    jobs, meta = await list_jobs(OPENAI, client)

    assert [job["id"] for job in jobs] == ["a"]
    assert meta.total == 1


@respx.mock
async def test_404_is_not_found_and_is_never_retried(client: Client):
    route = openai_route().mock(return_value=httpx.Response(404))

    with pytest.raises(NotFound) as excinfo:
        await fetch(OPENAI, client)

    assert excinfo.value.status == "not_found"
    assert route.call_count == 1


@respx.mock
async def test_truncated_body_is_parse_error_after_one_retry(client: Client):
    """§5.3: a truncated body raises `parse_error` and is retried once."""
    truncated = json.dumps(board("openai"))[:2000]
    route = openai_route().mock(
        return_value=httpx.Response(
            200, content=truncated, headers={"content-type": "application/json"}
        )
    )

    with pytest.raises(ParseError) as excinfo:
        await fetch(OPENAI, client)

    assert excinfo.value.status == "parse_error"
    assert route.call_count == 2


@respx.mock
async def test_malformed_shape_is_parse_error(client: Client):
    """200 with no `jobs` array is not an empty board — it is a provider that moved."""
    openai_route().mock(return_value=httpx.Response(200, json={"apiVersion": "1"}))

    with pytest.raises(ParseError):
        await list_jobs(OPENAI, client)


@respx.mock
async def test_empty_board_is_zero_jobs_not_an_error(client: Client):
    """§5.12: 200-with-zero-jobs is `ok, jobsFound: 0` per company; the degradation guard
    is a population-level rule, not a per-company one."""
    openai_route().mock(return_value=httpx.Response(200, json={"jobs": [], "apiVersion": "1"}))

    jobs, meta = await list_jobs(OPENAI, client)

    assert jobs == [] and meta.total == 0
