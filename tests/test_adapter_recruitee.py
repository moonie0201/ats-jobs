"""Recruitee adapter (SPEC v2 §5.6, §4.5, §10.1).

Every fixture under ``tests/fixtures/recruitee/`` is a real ``GET
https://{slug}.recruitee.com/api/offers/`` response, captured live and truncated to the
first offers. Three boards on purpose: `channable` carries structured pay on every offer,
`nmbrs` carries the all-null ``salary`` object a board with no pay data sends, and
`vandebron` is the only sampled board with ``on_site`` roles, part-time codes and a
Dutch-language ``country`` ("Nederland") for the §4.5.1 step-1 upper-case rule.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from core.http import Client, HttpError, NotFound, ParseError, make_client
from core.models import JobRecord, Ref
from core.providers import get_adapter
from core.providers.recruitee import (
    LIST_URL,
    NOT_HOSTED_MESSAGE,
    SPEC,
    fetch,
    to_record,
)

OPTIONS: dict[str, Any] = {
    "includeDescription": True,
    "descriptionFormat": "both",
    "redactContacts": True,
}


def ref(slug: str) -> Ref:
    return Ref(provider="recruitee", slug=slug, input=f"{slug}.recruitee.com")


def records(fixture, slug: str, options: dict[str, Any] | None = None) -> list[JobRecord]:
    payload = fixture("recruitee", f"{slug}.json")
    return [to_record(offer, ref(slug), options or OPTIONS) for offer in payload["offers"]]


@pytest.fixture
def client() -> Client:
    """Instant backoffs and an instant rate cap: no test ever really sleeps."""
    clock = [0.0]

    async def fake_sleep(delay: float) -> None:
        clock[0] += delay

    return make_client(timeout_secs=5, sleep=fake_sleep, clock=lambda: clock[0])


# --- §10.1: exact values for the first record -------------------------------------------


def test_first_channable_record_matches_the_live_payload(fixture):
    job = records(fixture, "channable")[0]

    assert job.id == "recruitee:channable:2715078"
    assert job.sourceId == "2715078"
    assert job.title == "Product Manager - Core AI"
    assert job.city == "Utrecht"
    assert job.countryCode == "NL"
    assert job.remote is False
    assert job.employmentType == "full_time"
    assert job.employmentTypeSource == "ats"
    assert job.salaryMin == 4500.0
    assert job.salaryMax == 6000.0
    assert job.salaryCurrency == "EUR"
    assert job.salaryInterval == "month"
    assert job.salarySource == "ats"
    assert job.postedAt == "2026-08-19T10:48:22Z"
    assert job.url == "https://jobs.channable.com/o/product-manager-core-ai"


def test_first_channable_record_fills_the_rest_of_the_5_6_mapping(fixture):
    job = records(fixture, "channable")[0]

    assert job.provider == "recruitee" and job.companySlug == "channable"
    assert job.company == "Channable"  # `company_name`
    assert job.department == "Product"
    assert job.locationRaw == "Utrecht"  # `locations[0].name`, not the joined `location`
    assert job.region == "Utrecht" and job.country == "Netherlands"
    assert job.workplaceType == "hybrid" and job.remoteSource == "ats"
    assert job.employmentTypeRaw == "fulltime_permanent"
    assert job.applyUrl == "https://jobs.channable.com/o/product-manager-core-ai/c/new"
    assert job.postedAtSource == "published_at"
    assert job.updatedAt == "2026-08-24T09:33:01Z"
    assert job.input == "channable.recruitee.com"
    assert job.warnings == []


def test_first_nmbrs_record_matches_the_live_payload(fixture):
    job = records(fixture, "nmbrs")[0]

    assert job.id == "recruitee:nmbrs:2721844"
    assert job.title == "Information Security Officer"
    assert job.city == "Amsterdam"
    assert job.countryCode == "NL"
    assert job.remote is False and job.workplaceType == "hybrid"
    assert job.employmentType == "full_time"
    assert job.employmentTypeSource == "ats"
    assert job.employmentTypeRaw == "fulltime_fixed_term"
    assert job.postedAt == "2026-08-25T13:42:17Z"
    assert job.url == "https://nmbrs.recruitee.com/o/information-security-officer"
    assert job.company == "Nmbrs BV"
    assert job.department is None  # the board sets no department; never invented


# --- provider-specific edge cases -------------------------------------------------------


def test_all_null_salary_object_is_not_an_ats_answer(fixture):
    """`nmbrs` sends ``salary: {min: null, max: null, period: null, currency: null}`` on
    every offer. A dict is enough for §4.5.3 step 1 to claim ``ats`` with no numbers in
    it, which would both lie about provenance and suppress step 2."""
    job = records(fixture, "nmbrs")[0]

    assert (job.salaryMin, job.salaryMax, job.salaryCurrency, job.salaryInterval) == (
        None,
        None,
        None,
        None,
    )
    assert job.salarySource is None


def test_partial_salary_band_is_still_an_ats_answer(fixture):
    """A minimum with no maximum is real pay data — ``15 EUR/hour`` on a Dutch side job."""
    job = next(j for j in records(fixture, "vandebron") if j.sourceId == "2117052")

    assert job.salaryMin == 15.0
    assert job.salaryMax is None
    assert job.salaryCurrency == "EUR"
    assert job.salaryInterval == "hour"
    assert job.salarySource == "ats"


def test_structured_location_wins_and_normalizes_the_country(fixture):
    """§4.5.1 step 1: ``country_code`` upper-cased, and the English name derived from it —
    `vandebron` sends the Dutch ``"Nederland"``. The raw name survives verbatim."""
    job = next(j for j in records(fixture, "vandebron") if j.sourceId == "2117052")

    assert job.locationRaw == "Amsterdam, 1013 KS"
    assert job.city == "Amsterdam"
    assert job.region == "Noord-Holland"
    assert job.country == "Netherlands"
    assert job.countryCode == "NL"
    assert [loc.raw for loc in job.locations] == ["Amsterdam, 1013 KS"]


def test_workplace_flags_are_read_from_the_ats(fixture):
    """§4.5.2 rank 1: ``remote`` / ``hybrid`` / ``on_site``. Neither of the two that fire
    on these boards may ever yield ``remote=True``."""
    jobs = records(fixture, "vandebron")
    seen = {(job.remote, job.workplaceType, job.remoteSource) for job in jobs}

    assert seen == {(False, "hybrid", "ats"), (False, "onsite", "ats")}


def test_compound_employment_codes_map_to_the_schedule_half(fixture):
    """Live codes are ``{schedule}_{permanence}``, never the bare ``fulltime`` §4.5.4
    tabulates. The permanence half is a contract detail, kept in ``employmentTypeRaw``."""
    jobs = records(fixture, "vandebron")
    seen = {(job.employmentTypeRaw, job.employmentType) for job in jobs}

    assert seen == {
        ("fulltime_permanent", "full_time"),
        ("fulltime_fixed_term", "full_time"),
        ("parttime_fixed_term", "part_time"),
    }
    assert all(job.employmentTypeSource == "ats" for job in jobs)


def test_unknown_employment_code_is_other_with_the_raw_preserved():
    job = to_record({"id": 1, "employment_type_code": "seasonal_gig"}, ref("acme"), OPTIONS)

    assert job.employmentType == "other"
    assert job.employmentTypeRaw == "seasonal_gig"
    assert job.employmentTypeSource == "ats"


def test_description_is_description_plus_requirements(fixture):
    """§5.6: both fields are inline HTML and both belong in ``descriptionHtml`` — no
    detail call exists to fetch the half that would otherwise be dropped."""
    payload = fixture("recruitee", "channable.json")
    offer = payload["offers"][0]
    job = to_record(offer, ref("channable"), OPTIONS)

    assert offer["description"][:60] in job.descriptionHtml
    assert offer["requirements"][:60] in job.descriptionHtml
    assert job.descriptionText and "<p" not in job.descriptionText


def test_dates_carry_recruitees_trailing_zone_word(fixture):
    """``"2026-08-19 10:48:22 UTC"`` is not ISO 8601; unconverted it yields a null date on
    every Recruitee job."""
    jobs = records(fixture, "vandebron")

    assert all(job.postedAt and job.postedAt.endswith("Z") for job in jobs)
    assert all(job.postedAtSource == "published_at" for job in jobs)
    assert next(j for j in jobs if j.sourceId == "2117052").postedAt == "2025-08-04T12:08:15Z"


def test_ids_are_stable_and_unique_across_a_board(fixture):
    jobs = records(fixture, "vandebron")

    assert len({job.id for job in jobs}) == len(jobs)
    assert all(job.id.startswith("recruitee:vandebron:") for job in jobs)
    assert [job.changeHash for job in jobs] == [j.changeHash for j in records(fixture, "vandebron")]


def test_raw_json_is_the_untouched_offer(fixture):
    """The pay-blanking in :func:`_pay_job` must not reach ``includeRawJson`` output."""
    payload = fixture("recruitee", "nmbrs.json")
    offer = payload["offers"][0]
    job = to_record(offer, ref("nmbrs"), {**OPTIONS, "includeRawJson": True})

    assert job.raw == offer
    assert job.raw["salary"] == {"max": None, "min": None, "period": None, "currency": None}


# --- §10.1 test_adapters_empty_objects --------------------------------------------------


EMPTY_OFFERS: tuple[dict[str, Any], ...] = (
    {},
    {"id": None, "title": None, "salary": {}, "locations": [], "department": None},
    {
        "id": "",
        "title": "",
        "salary": {"min": None, "max": None, "period": None, "currency": None},
        "locations": [{}],
        "department": {},
        "company_name": None,
        "employment_type_code": None,
        "careers_url": None,
        "published_at": None,
        "description": None,
        "requirements": None,
    },
)


@pytest.mark.parametrize("offer", EMPTY_OFFERS)
def test_empty_objects_yield_nulls_not_exceptions(offer: dict[str, Any]):
    """Every nested provider object empty or absent: no exception, all-null output. This
    is the test that catches the day Recruitee reshapes a field."""
    job = to_record(offer, ref("acme"), OPTIONS)

    assert job.recordType == "job"
    assert job.provider == "recruitee" and job.companySlug == "acme"
    assert job.id == "recruitee:acme:"
    for field in (
        "title",
        "company",
        "department",
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
        "updatedAt",
        "descriptionHtml",
        "descriptionText",
    ):
        assert getattr(job, field) is None, field
    assert job.locations == []


def test_a_non_dict_offer_does_not_raise():
    assert to_record(None, ref("acme"), OPTIONS).title is None  # type: ignore[arg-type]


# --- fetch: transport and §5.6 failure semantics ----------------------------------------


@respx.mock
async def test_fetch_reads_every_offer_in_one_request(fixture, client: Client):
    """§5.6: no pagination and no detail call — one GET per company, ever."""
    payload = fixture("recruitee", "channable.json")
    route = respx.get(LIST_URL.format(slug="channable")).mock(
        return_value=httpx.Response(200, json=payload)
    )

    jobs = await fetch(ref("channable"), client, options=OPTIONS)

    assert route.call_count == 1
    assert len(jobs) == len(payload["offers"])
    assert jobs[0].id == "recruitee:channable:2715078"
    assert SPEC.needs_detail_call is False
    assert SPEC.name == "recruitee" and SPEC.host_rate_limit == 2.0


@respx.mock
async def test_fetch_skips_non_object_entries(client: Client):
    respx.get(LIST_URL.format(slug="acme")).mock(
        return_value=httpx.Response(200, json={"offers": [{"id": 1, "title": "Dev"}, None, "x"]})
    )

    jobs = await fetch(ref("acme"), client)

    assert [job.title for job in jobs] == ["Dev"]


@respx.mock
async def test_empty_board_yields_no_records(client: Client):
    respx.get(LIST_URL.format(slug="acme")).mock(
        return_value=httpx.Response(200, json={"offers": []})
    )

    assert await fetch(ref("acme"), client) == []


@respx.mock
async def test_404_is_not_found(fixture, client: Client):
    body = fixture("recruitee", "not_found.json")
    route = respx.get(LIST_URL.format(slug="nope")).mock(
        return_value=httpx.Response(404, json=body)
    )

    with pytest.raises(NotFound) as caught:
        await fetch(ref("nope"), client)

    assert caught.value.status == "not_found"
    assert route.call_count == 1  # a 404 is never retried


@respx.mock
async def test_301_to_careers_not_hosted_is_not_found_with_the_spec_message(client: Client):
    """The redirect target is a marketing page: following it would turn a missing board
    into a healthy one with zero jobs (§5.6, §6.3)."""
    respx.get(LIST_URL.format(slug="moved")).mock(
        return_value=httpx.Response(
            301, headers={"location": "https://recruitee.com/careers_not_hosted"}
        )
    )

    with pytest.raises(NotFound) as caught:
        await fetch(ref("moved"), client)

    assert str(caught.value) == NOT_HOSTED_MESSAGE
    assert caught.value.status == "not_found"
    assert caught.value.http_status == 301


@respx.mock
async def test_200_body_carrying_only_an_error_is_not_found(client: Client):
    respx.get(LIST_URL.format(slug="nope")).mock(
        return_value=httpx.Response(200, json={"error": "Not Found"})
    )

    with pytest.raises(NotFound) as caught:
        await fetch(ref("nope"), client)

    assert caught.value.status == "not_found"


@respx.mock
async def test_422_is_an_http_error(client: Client):
    route = respx.get(LIST_URL.format(slug="acme")).mock(
        return_value=httpx.Response(422, json={"error": "Unprocessable Entity"})
    )

    with pytest.raises(HttpError) as caught:
        await fetch(ref("acme"), client)

    assert caught.value.status == "http_error"
    assert caught.value.http_status == 422
    assert route.call_count == 1  # a 4xx is a verdict, not a hiccup


@respx.mock
async def test_truncated_body_is_a_parse_error_after_one_retry(client: Client):
    route = respx.get(LIST_URL.format(slug="acme")).mock(
        return_value=httpx.Response(200, content=b'{"offers": [{"id":')
    )

    with pytest.raises(ParseError) as caught:
        await fetch(ref("acme"), client)

    assert caught.value.status == "parse_error"
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize("body", [[], {"jobs": []}, {"offers": {}}])
async def test_a_payload_without_an_offers_array_is_a_parse_error(body: Any, client: Client):
    respx.get(LIST_URL.format(slug="acme")).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(ParseError):
        await fetch(ref("acme"), client)


def test_the_registry_serves_this_adapter():
    module = get_adapter("recruitee")

    assert module.SPEC is SPEC
    assert module.fetch is fetch and module.to_record is to_record
