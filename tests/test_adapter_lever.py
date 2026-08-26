"""Lever adapter (SPEC v2 §5.2, §4.5, §10.1). Fixtures are real captures; respx serves HTTP.

``tests/fixtures/lever/`` holds four live payloads, all captured 2026-08-26 from the public
endpoint:

===================== ===========================================================
``palantir.json``     first 12 of 308 postings, ``api.lever.co`` — 0 structured salaries
``matchgroup.json``   first 8 of 78, ``api.lever.co`` — ``salaryRange`` populated
``global_empty.json`` ``api.lever.co/v0/postings/lever`` -> ``[]`` (200, not 404)
``eu_only.json``      ``api.eu.lever.co/v0/postings/lever`` -> the same board's 6 jobs
===================== ===========================================================

The last two are the pair §5.2 was corrected around (V2 T-H6): a board that is 200-empty
globally and populated on EU. They are captured from one slug on two hosts, so the
fallback test runs against exactly what production sees.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from core.http import NotFound, ParseError, make_client
from core.models import Ref
from core.providers import get_adapter
from core.providers.lever import SPEC, fetch, list_jobs, to_record

GLOBAL = "https://api.lever.co/v0/postings/{slug}"
EU = "https://api.eu.lever.co/v0/postings/{slug}"


@pytest.fixture
def client():
    """Instant backoffs and an instant rate cap; no wall-clock in the suite."""
    clock = [0.0]

    async def fake_sleep(delay: float) -> None:
        clock[0] += delay

    return make_client(timeout_secs=5, sleep=fake_sleep, clock=lambda: clock[0])


@pytest.fixture
def palantir(fixture):
    return fixture("lever", "palantir.json")


@pytest.fixture
def matchgroup(fixture):
    return fixture("lever", "matchgroup.json")


def ref(slug: str = "palantir", **kwargs) -> Ref:
    return Ref(provider="lever", slug=slug, **kwargs)


# --- §10.1: exact expected values for the first record ---------------------------------------


def test_first_record_exact_values(palantir):
    job = to_record(palantir[0], ref())

    assert job.id == "lever:palantir:ac978161-6f46-4f6b-ad9e-a258e642751c"
    assert job.sourceId == "ac978161-6f46-4f6b-ad9e-a258e642751c"
    assert job.title == "Administrative Business Partner"
    assert job.city == "London"
    assert job.countryCode == "GB"
    assert job.country == "United Kingdom"
    assert job.remote is False
    assert job.workplaceType == "hybrid"
    assert job.remoteSource == "ats"
    assert job.employmentType == "full_time"
    assert job.employmentTypeRaw == "Full-time"
    assert job.employmentTypeSource == "ats"
    # palantir publishes no structured pay: 0 of 307 postings carry `salaryRange`
    # (V2 T-L3), and nothing in the body survives §4.5.3's rejection gates.
    assert (job.salaryMin, job.salaryMax) == (None, None)
    assert (job.salaryCurrency, job.salaryInterval, job.salarySource) == (None, None, None)
    assert job.postedAt == "2024-03-25T21:50:16Z"
    assert job.postedAtSource == "createdAt"
    assert job.url == "https://jobs.lever.co/palantir/ac978161-6f46-4f6b-ad9e-a258e642751c"
    assert job.applyUrl == job.url + "/apply"
    assert job.provider == "lever" and job.companySlug == "palantir"


def test_department_falls_back_to_team(palantir):
    """The first palantir posting has `categories.team` and no `categories.department`."""
    assert "department" not in palantir[0]["categories"]
    job = to_record(palantir[0], ref())
    assert job.department == "Administrative"
    assert job.team == "Administrative"


def test_department_and_team_are_distinct_when_lever_gives_both(matchgroup):
    job = to_record(matchgroup[0], ref("matchgroup"))
    assert (job.department, job.team) == ("Hyperconnect", "Management")
    assert job.employmentType == "contract"
    assert job.title == "Accountant (1년 6개월 계약직)"


def test_structured_salary_from_salary_range(matchgroup):
    """`salaryRange{min,max,currency,interval}` -> `salarySource="ats"` (§4.5.3 step 1)."""
    job = to_record(matchgroup[1], ref("matchgroup"))
    assert job.title == "Android Engineer III"
    assert (job.salaryMin, job.salaryMax) == (150000.0, 180000.0)
    assert job.salaryCurrency == "USD"
    assert job.salaryInterval == "year"  # from "per-year-salary"
    assert job.salarySource == "ats"
    assert job.salaryRaw == matchgroup[1]["salaryDescription"]  # §5.2: salaryDescription


def test_locations_are_sorted_and_deduped(matchgroup):
    """`categories.allLocations[]` feeds `locations[]`, sorted per §4.5.1 step 8."""
    job = to_record(matchgroup[7], ref("matchgroup"))
    assert matchgroup[7]["categories"]["allLocations"] == [
        "New York, New York",
        "Los Angeles, California",
    ]
    assert [loc.city for loc in job.locations] == ["Los Angeles", "New York"]
    assert job.locationRaw == "New York, New York"  # `categories.location` stays primary
    assert job.city == "New York"


def test_top_level_country_overrides_the_parsed_code(fixture):
    """§5.2: `country` is ISO2 and wins. "Toronto" alone parses to no country at all."""
    job = to_record(fixture("lever", "eu_only.json")[0], ref("lever", region="eu"))
    assert job.locationRaw == "Toronto"
    assert (job.city, job.countryCode, job.country) == ("Toronto", "CA", "Canada")
    # locations[] keeps what the location string itself parsed to.
    assert [(loc.city, loc.countryCode) for loc in job.locations] == [("Toronto", None)]


def test_country_is_upper_cased_and_never_half_populated():
    job = to_record({"id": "1", "text": "Dev", "country": "de"}, ref())
    assert (job.countryCode, job.country) == ("DE", "Germany")


def test_unspecified_workplace_type_falls_through(fixture):
    """Lever's `unspecified` means the customer never answered (§4.5.2 rank 1)."""
    payload = fixture("lever", "eu_only.json")[0]
    assert payload["workplaceType"] == "unspecified"
    job = to_record(payload, ref("lever", region="eu"))
    assert (job.remote, job.workplaceType, job.remoteSource) == (None, None, None)


def test_description_concatenates_body_lists_and_additional(palantir):
    raw = palantir[0]
    options = {
        "includeDescription": True,
        "descriptionFormat": "both",
        "redactContacts": False,  # §4.5.3 redaction would rewrite the strings compared here
    }
    job = to_record(raw, ref(), options)
    assert raw["description"] in job.descriptionHtml
    assert raw["lists"][0]["content"] in job.descriptionHtml
    assert raw["additional"] in job.descriptionHtml
    # V2 T-L4: `description` already contains `opening` + `descriptionBody`, so neither is
    # concatenated a second time.
    assert raw["descriptionBody"] in raw["description"]
    assert job.descriptionHtml.count(raw["descriptionBody"]) == 1
    assert job.descriptionText.startswith(raw["descriptionPlain"])
    assert job.descriptionText.endswith(raw["additionalPlain"].strip())
    assert not job.descriptionRedacted  # nothing removed, nothing claimed


def test_contacts_are_redacted_by_default(palantir):
    """`redactContacts` defaults on, and the adapter hands the body over before parsing."""
    raw = palantir[0]
    assert "accommodations@palantir.com" in raw["additional"]
    job = to_record(raw, ref(), {"includeDescription": True, "descriptionFormat": "both"})
    assert job.descriptionRedacted is True
    assert "accommodations@palantir.com" not in job.descriptionHtml
    assert "@palantir.com" not in job.descriptionText


def test_description_is_omitted_unless_asked_for(palantir):
    job = to_record(palantir[0], ref())
    assert job.descriptionHtml is None and job.descriptionText is None


def test_whole_fixture_maps_without_loss(palantir):
    records = [to_record(job, ref()) for job in palantir]
    assert len(records) == 12
    assert all(r.id and r.title and r.url and r.contentKey and r.changeHash for r in records)
    assert len({r.id for r in records}) == 12
    assert all(r.postedAt and r.postedAt.endswith("Z") for r in records)
    assert all(r.recordType == "job" for r in records)


# --- §10.1 test_adapters_empty_objects: every nested object {} or absent ----------------------

EMPTY_OBJECT_JOBS = [
    {},
    {"id": "", "text": "", "categories": {}},
    {"id": "abc", "text": "Engineer", "categories": {}},
    {
        "id": "abc",
        "text": "Engineer",
        "categories": {"department": None, "team": None, "location": None, "allLocations": None},
        "country": None,
        "workplaceType": None,
        "salaryRange": {},
        "salaryDescription": None,
        "lists": [],
        "createdAt": None,
    },
    # Wrong types, the shape a provider schema change actually arrives in.
    {"id": "abc", "text": "Engineer", "categories": [], "salaryRange": [], "lists": {}},
    {"id": "abc", "text": "Engineer", "categories": {"allLocations": "Berlin"}, "country": ""},
]


@pytest.mark.parametrize("job", EMPTY_OBJECT_JOBS)
def test_empty_objects_produce_nulls_not_exceptions(job):
    record = to_record(job, ref(), {"includeDescription": True})
    assert record.provider == "lever" and record.companySlug == "palantir"
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
        "url",
        "applyUrl",
        "postedAt",
        "postedAtSource",
        "descriptionHtml",
        "descriptionText",
    ):
        assert getattr(record, field) is None, field
    assert record.locations == []


def test_empty_objects_still_produce_a_stable_identity():
    a = to_record({"id": "abc", "text": "Engineer", "categories": {}}, ref())
    b = to_record({"id": "abc", "text": "Engineer", "categories": {}}, ref())
    assert a.id == b.id == "lever:palantir:abc"
    assert (a.contentKey, a.changeHash) == (b.contentKey, b.changeHash)


# --- §5.2 fetch: region probing, pagination, failures -----------------------------------------


@respx.mock
async def test_global_200_empty_falls_through_to_eu(client, fixture):
    """The V2 T-H6 correction: 200-with-[] is not "an empty company", it is "wrong host"."""
    eu_payload = fixture("lever", "eu_only.json")
    glob = respx.get(GLOBAL.format(slug="lever")).mock(
        return_value=httpx.Response(200, json=fixture("lever", "global_empty.json"))
    )
    eu = respx.get(EU.format(slug="lever")).mock(return_value=httpx.Response(200, json=eu_payload))

    company = ref("lever")
    records = await fetch(company, client)

    assert glob.called and eu.called
    assert len(records) == len(eu_payload) == 6
    assert records[0].title == "API Engineer"
    assert company.region == "eu"  # cached, so the next run probes EU first


@respx.mock
async def test_cached_eu_region_probes_eu_first(client, fixture):
    glob = respx.get(GLOBAL.format(slug="lever")).mock(return_value=httpx.Response(200, json=[]))
    eu = respx.get(EU.format(slug="lever")).mock(
        return_value=httpx.Response(200, json=fixture("lever", "eu_only.json"))
    )

    assert len(await fetch(ref("lever", region="eu"), client)) == 6
    assert eu.called and not glob.called


@respx.mock
async def test_global_hit_never_probes_eu(client, palantir):
    glob = respx.get(GLOBAL.format(slug="palantir")).mock(
        return_value=httpx.Response(200, json=palantir)
    )
    eu = respx.get(EU.format(slug="palantir")).mock(return_value=httpx.Response(200, json=[]))

    company = ref("palantir")
    assert len(await fetch(company, client)) == 12
    assert glob.called and not eu.called
    assert company.region is None


@respx.mock
async def test_both_hosts_empty_is_ok_with_zero_jobs_and_no_cached_region(client):
    respx.get(GLOBAL.format(slug="plaid")).mock(return_value=httpx.Response(200, json=[]))
    respx.get(EU.format(slug="plaid")).mock(return_value=httpx.Response(200, json=[]))

    company = ref("plaid")
    jobs, meta = await list_jobs(company, client)

    assert (jobs, meta.total) == ([], 0)
    assert company.region is None  # the board may fill later on either host


@respx.mock
async def test_404_on_both_hosts_is_not_found(client):
    body = {"ok": False, "error": "Document not found"}
    respx.get(GLOBAL.format(slug="netflix")).mock(return_value=httpx.Response(404, json=body))
    respx.get(EU.format(slug="netflix")).mock(return_value=httpx.Response(404, json=body))

    with pytest.raises(NotFound):
        await fetch(ref("netflix"), client)


@respx.mock
async def test_404_global_but_eu_serves_the_board(client, fixture):
    respx.get(GLOBAL.format(slug="lever")).mock(return_value=httpx.Response(404))
    respx.get(EU.format(slug="lever")).mock(
        return_value=httpx.Response(200, json=fixture("lever", "eu_only.json"))
    )
    company = ref("lever")
    assert len(await fetch(company, client)) == 6
    assert company.region == "eu"


@respx.mock
async def test_404_global_and_empty_eu_is_zero_jobs_not_not_found(client):
    respx.get(GLOBAL.format(slug="ghost")).mock(return_value=httpx.Response(404))
    respx.get(EU.format(slug="ghost")).mock(return_value=httpx.Response(200, json=[]))
    assert await fetch(ref("ghost"), client) == []


@respx.mock
async def test_unexpected_4xx_is_not_swallowed_as_an_empty_board(client):
    """422 and friends are `http_error` via the shared client, never `ok, 0 jobs` (§5.12)."""
    from core.http import HttpError

    respx.get(GLOBAL.format(slug="palantir")).mock(return_value=httpx.Response(422))
    with pytest.raises(HttpError):
        await fetch(ref("palantir"), client)


@respx.mock
async def test_malformed_body_retries_once_then_parse_error(client):
    route = respx.get(GLOBAL.format(slug="palantir")).mock(
        return_value=httpx.Response(200, text='[{"id": "abc", "text": "Eng"')
    )
    with pytest.raises(ParseError):
        await fetch(ref("palantir"), client)
    assert route.call_count == 2  # one retry, per §5.12


@respx.mock
async def test_a_json_object_instead_of_an_array_is_a_parse_error(client):
    """A 200 carrying `{"ok": false}` must not be read as a board with no jobs."""
    respx.get(GLOBAL.format(slug="palantir")).mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "Document not found"})
    )
    with pytest.raises(ParseError):
        await fetch(ref("palantir"), client)


@respx.mock
async def test_pagination_only_pages_when_a_page_comes_back_full(client, palantir):
    """`limit=1000&skip=N`, and only while a page is exactly full (§5.2)."""
    full = [dict(job, id=f"{i}") for i, job in enumerate(palantir * 84)][:1000]
    route = respx.get(GLOBAL.format(slug="palantir")).mock(
        side_effect=[
            httpx.Response(200, json=full),
            httpx.Response(200, json=[dict(palantir[0], id="tail")]),
        ]
    )

    records = await fetch(ref("palantir"), client)

    assert len(records) == 1001
    assert route.call_count == 2
    assert dict(route.calls[0].request.url.params) == {"mode": "json", "limit": "1000", "skip": "0"}
    assert dict(route.calls[1].request.url.params)["skip"] == "1000"


@respx.mock
async def test_slug_casing_is_preserved_in_the_request(client, palantir):
    """Lever is the one case-SENSITIVE provider: `Palantir` 404s where `palantir` works."""
    route = respx.get(GLOBAL.format(slug="MatchGroup")).mock(
        return_value=httpx.Response(200, json=palantir)
    )
    records = await fetch(ref("MatchGroup"), client)
    assert route.called
    assert records[0].companySlug == "MatchGroup"
    assert records[0].id.startswith("lever:matchgroup:")  # identity alone is lower-cased


# --- registry contract ------------------------------------------------------------------------


def test_provider_spec():
    assert SPEC.name == "lever"
    assert SPEC.host_rate_limit == 1.0  # robots.txt Crawl-delay: 1
    assert SPEC.needs_detail_call is False
    assert SPEC.ai_train is False  # Content-Signal ai-train=no, honoured by field
    assert SPEC.retainable is True


def test_registry_resolves_the_adapter():
    module = get_adapter("lever")
    assert module.fetch is fetch
    assert module.SPEC is SPEC
