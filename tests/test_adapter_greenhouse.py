"""Greenhouse adapter (SPEC v2 §5.1, §10.1). No network: respx serves every call.

Every fixture under `tests/fixtures/greenhouse/` is a real payload captured from
`boards-api.greenhouse.io` and committed, refreshable with `scripts/refresh_fixtures.py`.
The expected values below are therefore the provider's own live data, not invented ones:
`anthropic` (the §4.6 demo board, pay ranges populated, greenhouse-hosted `absolute_url`),
`stripe` (custom-domain `absolute_url`, org-coded departments, office rollup buckets) and
`airbnb` (non-US locale, EUR pay range).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from core.http import NotFound, ParseError, make_client
from core.models import Ref
from core.providers import greenhouse
from core.providers.greenhouse import SIZE_WARNING, fetch, normalize_department, to_record

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs"

#: `includeDescription` is on so the description-derived fields are actually exercised;
#: `both` keeps the HTML *and* the text so the entity-unescape assertion has something to
#: look at. Every other key falls back to the §4.1 defaults.
OPTIONS = {"includeDescription": True, "descriptionFormat": "both"}


@pytest.fixture
def anthropic(fixture):
    return fixture("greenhouse", "anthropic.json")["jobs"]


@pytest.fixture
def stripe(fixture):
    return fixture("greenhouse", "stripe_customdomain.json")["jobs"]


@pytest.fixture
def airbnb(fixture):
    return fixture("greenhouse", "airbnb_paytransparency.json")["jobs"]


@pytest.fixture
def client():
    """A client whose backoffs and rate-cap waits are instant."""
    clock = [0.0]

    async def fake_sleep(delay: float) -> None:
        clock[0] += delay

    return make_client(timeout_secs=5, sleep=fake_sleep, clock=lambda: clock[0])


# ------------------------------------------------------------------ §10.1 first record


def test_first_record_exact_values(anthropic):
    """The §10.1 assertion list, against the live anthropic board."""
    row = to_record(anthropic[0], Ref("greenhouse", "anthropic"), OPTIONS)

    assert row.id == "greenhouse:anthropic:4461450008"
    assert row.sourceId == "4461450008"
    assert row.title == "Account Executive, AI Native"
    assert row.city == "New York City"
    assert row.countryCode == "US"
    # Greenhouse reports no workplace flag and the location says nothing: null, not a guess.
    assert row.remote is None
    assert row.workplaceType is None and row.remoteSource is None
    # No employment field on the endpoint and no title keyword -> null, source null.
    assert row.employmentType is None
    assert row.employmentTypeRaw is None
    assert row.employmentTypeSource is None
    assert row.salaryMin == 222800.0
    assert row.salaryMax == 290000.0
    assert row.salaryCurrency == "USD"
    assert row.salaryInterval == "year"
    assert row.salarySource == "ats"
    assert row.postedAt == "2024-12-20T18:53:38Z"
    assert row.postedAtSource == "first_published"
    assert row.url == "https://job-boards.greenhouse.io/anthropic/jobs/4461450008"


def test_first_record_supporting_fields(anthropic):
    row = to_record(anthropic[0], Ref("greenhouse", "anthropic"), OPTIONS)
    assert row.provider == "greenhouse" and row.companySlug == "anthropic"
    assert row.company == "Anthropic"
    assert row.department == "Sales"
    assert row.locationRaw == "New York City, NY"
    assert row.region == "NY" and row.country == "United States"
    assert row.requisitionId == "3356"
    assert row.updatedAt == "2026-08-22T01:32:54Z"


def test_pay_transparency_populates_much_of_the_board(anthropic):
    """§5.1 measured coverage (472/533 on the full board, 12/25 in this committed slice):
    a silent regression in the `pay_transparency=true` param shows up here, not in
    production — and a fabricated range would show up as 25/25."""
    rows = [to_record(job, Ref("greenhouse", "anthropic")) for job in anthropic]
    with_pay = [r for r in rows if r.salarySource == "ats"]
    assert 10 <= len(with_pay) < len(rows)
    assert all(r.salaryCurrency and r.salaryInterval for r in with_pay)
    assert all(r.salaryMin is None and r.salaryMax is None for r in rows if r.salarySource is None)


def test_airbnb_first_record_non_us_locale(airbnb):
    """A second real board: the currency and country must come from the payload, not
    from a US-shaped default."""
    row = to_record(airbnb[0], Ref("greenhouse", "airbnb"), OPTIONS)
    assert row.id == "greenhouse:airbnb:7995199"
    assert row.title == "Acquisition Manager"
    assert row.city == "Paris" and row.countryCode == "FR"
    assert row.region is None
    assert row.remote is None
    assert row.employmentType is None and row.employmentTypeSource is None
    assert (row.salaryMin, row.salaryMax) == (61000.0, 72000.0)
    assert row.salaryCurrency == "EUR" and row.salaryInterval == "year"
    assert row.salarySource == "ats"
    assert row.postedAt == "2026-06-10T12:51:02Z"
    assert row.url == "https://careers.airbnb.com/positions/7995199?gh_jid=7995199"


# ------------------------------------------------------------- §10.1 provider edge cases


def test_content_is_html_unescaped_exactly_once(anthropic):
    """§5.1: `content` arrives entity-escaped (`'&lt;h2&gt;'`) and must be unescaped once."""
    raw = anthropic[0]["content"]
    assert raw.startswith("&lt;div")  # the fixture really is escaped

    row = to_record(anthropic[0], Ref("greenhouse", "anthropic"), OPTIONS)
    assert row.descriptionHtml.startswith('<div class="content-intro"><h2>')
    assert "&lt;" not in row.descriptionHtml  # not unescaped twice either
    # No assertion on the words: `scripts/scrub_fixtures.py` replaces employer prose with
    # filler, so the contract this test guards is the markup, not the copy.
    assert row.descriptionText and not row.descriptionText.startswith("<")
    assert "&lt;" not in row.descriptionText
    assert "<h2>" not in row.descriptionText


def test_description_is_absent_unless_asked_for(anthropic):
    row = to_record(anthropic[0], Ref("greenhouse", "anthropic"))
    assert row.descriptionHtml is None and row.descriptionText is None
    # …but it was still read: the structured pay range came off the same job.
    assert row.salarySource == "ats"


def test_custom_domain_url_is_taken_verbatim(stripe):
    """§5.1 correction 1: stripe publishes its own careers host, and `applyUrl` is the
    same value — the `#app` anchor v1 appended exists nowhere in the payload."""
    row = to_record(stripe[0], Ref("greenhouse", "stripe"), OPTIONS)
    assert row.url == "https://stripe.com/jobs/search?gh_jid=7532733"
    assert row.applyUrl == row.url
    assert "#app" not in row.applyUrl

    rows = [to_record(job, Ref("greenhouse", "stripe")) for job in stripe]
    assert all(r.url.startswith("https://stripe.com/") for r in rows)
    assert all(r.applyUrl == r.url for r in rows)


def test_greenhouse_hosted_url_is_also_verbatim(anthropic):
    rows = [to_record(job, Ref("greenhouse", "anthropic")) for job in anthropic]
    assert all(r.url.startswith("https://job-boards.greenhouse.io/anthropic/jobs/") for r in rows)


def test_org_code_stripped_from_department(stripe):
    """§10.1: `"1653 Startups - Account Executives (NA)"` -> `"Account Executives (NA)"`."""
    names = [job["departments"][0]["name"] for job in stripe]
    assert "1653 Startups - Account Executives (NA)" in names  # the spec's own sample

    rows = [to_record(job, Ref("greenhouse", "stripe")) for job in stripe]
    by_raw = dict(zip(names, [r.department for r in rows], strict=True))
    assert by_raw["1653 Startups - Account Executives (NA)"] == "Account Executives (NA)"
    assert by_raw["1175 Enterprise - Account Executives (NA)"] == "Account Executives (NA)"
    # No dash at all — the org code is still not a department name.
    assert by_raw["1195 Account Executives (APAC)"] == "Account Executives (APAC)"
    assert not any((r.department or "")[0].isdigit() for r in rows)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1653 Startups - Account Executives (NA)", "Account Executives (NA)"),
        ("1642 Product Sales — MaaS", "MaaS"),
        ("1195 Account Executives (APAC)", "Account Executives (APAC)"),
        ("81430 - Engineering", "Engineering"),
        # Left alone: no leading org code, or nothing left after stripping.
        ("Engineering", "Engineering"),
        ("3D Hardware", "3D Hardware"),
        ("G&A - Finance", "G&A - Finance"),
        ("1653 -", "1653 -"),
        ("", None),
        (None, None),
        ({}, None),
    ],
)
def test_normalize_department_cases(raw, expected):
    assert normalize_department(raw) == expected


def test_team_is_never_populated(stripe, anthropic, airbnb):
    """§5.1 correction 2: `departments` has length 1 on every live job, so v1's
    `departments[1].name -> team` rule could never fire."""
    for jobs, slug in ((stripe, "stripe"), (anthropic, "anthropic"), (airbnb, "airbnb")):
        assert all(len(job["departments"]) == 1 for job in jobs)
        assert all(to_record(job, Ref("greenhouse", slug)).team is None for job in jobs)


def test_office_names_never_enter_locations(stripe):
    """§5.1 correction 3: `offices[]` are org rollup buckets — `{"name": "US",
    "location": null}`. Feeding "US" (or "Japan Locations") to §4.5.1 invents a city."""
    assert stripe[0]["offices"][0] == {
        "id": 65234,
        "name": "US",
        "location": None,
        "child_ids": stripe[0]["offices"][0]["child_ids"],
        "parent_id": 673,
    }
    row = to_record(stripe[0], Ref("greenhouse", "stripe"), OPTIONS)
    assert [loc.raw for loc in row.locations] == ["San Francisco, CA"]

    office_names = {o["name"] for job in stripe for o in job["offices"]}
    seen = {loc.raw for job in stripe for loc in to_record(job, Ref("greenhouse", "s")).locations}
    assert not (office_names & seen)


def test_non_null_office_locations_do_enter_locations_sorted(anthropic):
    """The other half of correction 3: a real `offices[].location` string is kept, and
    `locations[]` comes back sorted (§4.5.1 step 8) so a reshuffle cannot flip changeHash."""
    row = to_record(anthropic[0], Ref("greenhouse", "anthropic"), OPTIONS)
    assert [loc.raw for loc in row.locations] == [
        "San Francisco, CA",
        "San Francisco, California, United States",
        "New York City, NY",
        "New York, New York, United States",
    ]
    assert [loc.sort_key for loc in row.locations] == sorted(loc.sort_key for loc in row.locations)
    assert row.locationRaw == "New York City, NY"  # the primary is still `location.name`


def test_records_are_stable_across_two_passes(anthropic):
    """Identity must not drift between runs of the same payload (§4.5.6)."""
    ref = Ref("greenhouse", "anthropic")
    first = [to_record(job, ref, OPTIONS) for job in anthropic]
    second = [to_record(job, ref, OPTIONS) for job in anthropic]
    assert [r.id for r in first] == [r.id for r in second]
    assert [r.contentKey for r in first] == [r.contentKey for r in second]
    assert [r.changeHash for r in first] == [r.changeHash for r in second]
    assert len({r.id for r in first}) == len(first)


# ------------------------------------------------------- §10.1 test_adapters_empty_objects

#: Every nested provider object empty or absent — the schema-change fixture of §10.1.
EMPTY_OBJECTS_JOB = {
    "id": None,
    "title": None,
    "absolute_url": None,
    "content": None,
    "requisition_id": None,
    "company_name": None,
    "first_published": None,
    "updated_at": None,
    "location": {},
    "departments": [{}],
    "offices": [{}, {"name": "US", "location": None}],
    "pay_input_ranges": [],
    "metadata": None,
}


@pytest.mark.parametrize("job", [EMPTY_OBJECTS_JOB, {}, None, {"departments": None, "offices": {}}])
def test_empty_objects_yield_all_null_and_never_raise(job):
    row = to_record(job, Ref("greenhouse", "acme"), OPTIONS)
    # V1 L10: a job with no `sourceId` has no id, and `make_id` says so. It used to return
    # the truthy `"greenhouse:acme:"`, so `dedupe`'s `if record.id:` guard always fired and
    # every id-less job on a board shared one key — all but the first silently dropped as
    # duplicates. An id we cannot build is not an id every job shares.
    assert row.id == ""
    assert row.title is None and row.titleNormalized is None
    assert row.company is None and row.department is None and row.team is None
    assert row.locationRaw is None and row.city is None and row.countryCode is None
    assert row.locations == []
    assert row.remote is None and row.workplaceType is None and row.remoteSource is None
    assert row.employmentType is None and row.employmentTypeSource is None
    assert row.salaryMin is None and row.salaryMax is None and row.salarySource is None
    assert row.postedAt is None and row.postedAtSource is None and row.updatedAt is None
    assert row.url is None and row.applyUrl is None
    assert row.descriptionHtml is None and row.descriptionText is None
    assert row.scrapedAt.endswith("Z")


def test_partial_schema_change_keeps_the_fields_that_survive():
    """A provider dropping one key must cost that field, not the whole row."""
    row = to_record(
        {"id": 7, "title": "Engineer", "location": {}, "departments": [{"name": None}]},
        Ref("greenhouse", "acme"),
    )
    assert row.id == "greenhouse:acme:7" and row.title == "Engineer"
    assert row.department is None and row.city is None


# ------------------------------------------------------------------------ fetch (§5.1, §5.12)


@respx.mock
async def test_fetch_calls_the_documented_endpoint_once(client, fixture):
    """One request per board: §5.1 verified `meta.total == len(jobs)`, so no pagination."""
    payload = fixture("greenhouse", "anthropic.json")
    route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    rows = await fetch(Ref("greenhouse", "anthropic"), client, options=OPTIONS)

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.url.params["content"] == "true"
    assert request.url.params["pay_transparency"] == "true"
    assert len(rows) == len(payload["jobs"])
    assert rows[0].id == "greenhouse:anthropic:4461450008"
    assert all(r.warnings == [] for r in rows)


@respx.mock
async def test_fetch_preserves_slug_casing_in_the_url(client):
    route = respx.get("https://boards-api.greenhouse.io/v1/boards/Anthropic/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [], "meta": {"total": 0}})
    )
    assert await fetch(Ref("greenhouse", "Anthropic"), client) == []
    assert route.call_count == 1


@respx.mock
async def test_404_is_not_found_and_is_never_retried(client, fixture):
    body = fixture("greenhouse", "404.json")
    route = respx.get(BOARD_URL).mock(return_value=httpx.Response(404, json=body))
    with pytest.raises(NotFound):
        await fetch(Ref("greenhouse", "anthropic"), client)
    assert route.call_count == 1


@respx.mock
async def test_malformed_body_is_parse_error(client):
    route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(ParseError):
        await fetch(Ref("greenhouse", "anthropic"), client)
    assert route.call_count == 2  # one soft retry (§5.12), then parse_error


@respx.mock
async def test_payload_without_jobs_array_is_parse_error_not_an_empty_board(client):
    """A 200 that lost its `jobs[]` must not be recorded as "this company is not hiring"."""
    respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json={"meta": {"total": 12}}))
    with pytest.raises(ParseError):
        await fetch(Ref("greenhouse", "anthropic"), client)


@respx.mock
async def test_empty_board_is_an_empty_list_not_an_error(client):
    respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json={"jobs": [], "meta": {}}))
    assert await fetch(Ref("greenhouse", "anthropic"), client) == []


@respx.mock
async def test_oversize_board_refetches_without_content(client, fixture, monkeypatch):
    """§5.1: past 40 MB the board is re-requested without `content=true` and every row
    carries the `description_omitted_size` warning."""
    payload = fixture("greenhouse", "anthropic.json")
    monkeypatch.setattr(greenhouse, "MAX_BODY_BYTES", 1024)
    route = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=payload))

    rows = await fetch(Ref("greenhouse", "anthropic"), client, options=OPTIONS)

    assert route.call_count == 2
    assert "content" not in route.calls[1].request.url.params
    assert route.calls[1].request.url.params["pay_transparency"] == "true"
    assert len(rows) == len(payload["jobs"])
    assert all(r.warnings == [SIZE_WARNING] for r in rows)
    # The description is dropped, the paid-for salary is not.
    assert rows[0].salarySource == "ats"


@respx.mock
async def test_retries_5xx_then_succeeds(client, fixture):
    payload = fixture("greenhouse", "anthropic.json")
    route = respx.get(BOARD_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=payload),
        ]
    )
    rows = await fetch(Ref("greenhouse", "anthropic"), client)
    assert route.call_count == 2 and len(rows) == len(payload["jobs"])


# ------------------------------------------------------------ §7.4 second signal


@respx.mock
async def test_posting_alive_is_404_gone_200_live_and_raises_on_anything_else(client):
    from core.http import FetchError

    route = respx.get(f"{BOARD_URL}/4019283").mock(return_value=httpx.Response(404))
    ref = Ref("greenhouse", "anthropic")
    assert await greenhouse.posting_alive(client, ref, "4019283") is False
    route.mock(return_value=httpx.Response(200, json={"id": 4019283}))
    assert await greenhouse.posting_alive(client, ref, "4019283") is True
    route.mock(return_value=httpx.Response(503))
    with pytest.raises(FetchError):  # unknown is the caller's call, never "gone"
        await greenhouse.posting_alive(client, ref, "4019283")


# ------------------------------------------------- list-only department enrichment (§7.7)

#: Captured live 2026-08-29 from `GET /boards/{slug}/departments` and trimmed to the
#: departments that hold the board fixtures' job ids (plus their parents, so Stripe's real
#: two-level tree survives). Job 5101378008 of `anthropic.json` had closed by then, which
#: is the natural "listed but under no department" case.
DEPARTMENTS_URL = "https://boards-api.greenhouse.io/v1/boards/anthropic/departments"
LIST_ONLY = {"listOnly": True}


def _list_only_board(payload: dict) -> dict:
    """What the board API really returns without `content=true`: no `departments`, no
    `offices`, no `content` (verified live 2026-08-29)."""
    strip = ("departments", "offices", "content")
    return {
        **payload,
        "jobs": [{k: v for k, v in j.items() if k not in strip} for j in payload["jobs"]],
    }


@respx.mock
async def test_list_only_fetches_departments_and_matches_the_content_true_department(
    client, fixture
):
    """The list-only `dept` must equal what `content=true` would have said, job for job."""
    full = fixture("greenhouse", "anthropic.json")
    board = respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_list_only_board(full)))
    depts = respx.get(DEPARTMENTS_URL).mock(
        return_value=httpx.Response(200, json=fixture("greenhouse", "anthropic_departments.json"))
    )

    rows = await fetch(Ref("greenhouse", "anthropic"), client, options=LIST_ONLY)

    assert board.call_count == 1 and depts.call_count == 1
    assert "content" not in board.calls[0].request.url.params
    expected = {
        r.sourceId: r.department
        for r in (to_record(j, Ref("greenhouse", "anthropic")) for j in full["jobs"])
    }
    got = {r.sourceId: r.department for r in rows}
    assert got.pop("5101378008") is None  # closed since the capture: no department, no guess
    expected.pop("5101378008")
    assert got == expected
    assert got["4461450008"] == "Sales"
    assert all(r.warnings == [] for r in rows)


@respx.mock
async def test_list_only_department_is_the_leaf_of_a_nested_tree_with_org_code_stripped(
    client, fixture
):
    """Stripe's tree: `"1175 Enterprise - Account Executives (NA)"` under `"GEO Sales (NA)
    (Planning Group)"`. The leaf names the job and the org code goes (§5.1 correction 2)."""
    full = fixture("greenhouse", "stripe_customdomain.json")
    respx.get("https://boards-api.greenhouse.io/v1/boards/stripe/jobs").mock(
        return_value=httpx.Response(200, json=_list_only_board(full))
    )
    respx.get("https://boards-api.greenhouse.io/v1/boards/stripe/departments").mock(
        return_value=httpx.Response(200, json=fixture("greenhouse", "stripe_departments.json"))
    )

    rows = await fetch(Ref("greenhouse", "stripe"), client, options=LIST_ONLY)

    by_id = {r.sourceId: r.department for r in rows}
    assert by_id["7532733"] == "Account Executives (NA)"
    assert by_id["8041076"] == "MaaS"
    assert "Planning Group" not in " ".join(filter(None, by_id.values()))
    assert by_id == {
        r.sourceId: r.department
        for r in (to_record(j, Ref("greenhouse", "stripe")) for j in full["jobs"])
    }


def _dept(id_, name, jobs, *, parent=None, children=()):
    return {
        "id": id_,
        "name": name,
        "parent_id": parent,
        "child_ids": list(children),
        "jobs": [{"id": j} for j in jobs],
    }


def test_departments_by_job_picks_the_most_specific_and_joins_siblings_deterministically():
    """Never observed live (0 of ~5,000 jobs on five boards), so pinned synthetically: a
    rollup parent that repeats its child's job loses to the child; two unrelated
    departments are name-sorted and joined, whatever order Greenhouse serialises."""
    payload = {
        "departments": [
            _dept(1, "Engineering", [10, 11], children=[2]),
            _dept(2, "Platform", [10], parent=1),
            _dept(3, "Sales", [11, 12]),
            _dept(4, "Ops", [], children=[]),
            {"id": 5, "name": "Broken", "jobs": None},
            {},
        ]
    }
    by_job = greenhouse.departments_by_job(httpx.Response(200, json=payload))
    assert [d["name"] for d in by_job["10"]] == ["Platform"]
    assert sorted(d["name"] for d in by_job["11"]) == ["Engineering", "Sales"]
    assert [d["name"] for d in by_job["12"]] == ["Sales"]
    assert "13" not in by_job

    row = to_record({"id": 11, "departments": by_job["11"]}, Ref("greenhouse", "acme"))
    assert row.department == "Engineering / Sales"
    reversed_row = to_record(
        {"id": 11, "departments": by_job["11"][::-1]}, Ref("greenhouse", "acme")
    )
    assert reversed_row.department == row.department


def test_departments_payload_without_the_array_is_a_parse_error():
    with pytest.raises(ValueError):
        greenhouse.departments_by_job(httpx.Response(200, json={"meta": {}}))
    with pytest.raises(ValueError):
        greenhouse.departments_by_job(httpx.Response(200, json=[]))


@pytest.mark.parametrize(
    "responses",
    [
        [httpx.Response(404, json={"error": "not found"})],
        [httpx.Response(503)],
        [httpx.Response(429, headers={"Retry-After": "60"})],
        [httpx.Response(200, text="<html>nope</html>")] * 2,
        [httpx.Response(200, json={"meta": {}})] * 2,
    ],
    ids=["404", "5xx", "429-retry-after-60", "malformed", "no-departments-array"],
)
@respx.mock
async def test_departments_failure_degrades_to_null_with_a_warning_never_fails_the_board(
    client, fixture, responses
):
    """§7.4: a lost board day is the product's worst defect, a missing department its
    second worst. The board stands, every row is dept=null and says why -- after **one**
    ask: a 429 whose `Retry-After: 60` the §5.12 table honoured three times would outlast
    the snapshot's 120 s company budget and turn the list result it already holds into a
    timeout, i.e. the lost board day this call exists not to cause."""
    full = fixture("greenhouse", "anthropic.json")
    respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_list_only_board(full)))
    depts = respx.get(DEPARTMENTS_URL).mock(side_effect=responses)

    rows = await fetch(Ref("greenhouse", "anthropic"), client, options=LIST_ONLY)

    assert depts.call_count == len(responses)  # no hard retry; only the soft body retry
    assert len(rows) == len(full["jobs"])
    assert all(r.department is None for r in rows)
    assert all(r.warnings == [greenhouse.DEPARTMENTS_WARNING] for r in rows)
    assert rows[0].title == full["jobs"][0]["title"]


@respx.mock
async def test_empty_board_skips_the_departments_call(client):
    respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json={"jobs": [], "meta": {}}))
    depts = respx.get(DEPARTMENTS_URL).mock(
        return_value=httpx.Response(200, json={"departments": []})
    )
    assert await fetch(Ref("greenhouse", "anthropic"), client, options=LIST_ONLY) == []
    assert depts.call_count == 0


@respx.mock
async def test_content_true_path_never_calls_departments(client, fixture):
    respx.get(BOARD_URL).mock(
        return_value=httpx.Response(200, json=fixture("greenhouse", "anthropic.json"))
    )
    depts = respx.get(DEPARTMENTS_URL).mock(
        return_value=httpx.Response(200, json={"departments": []})
    )
    await fetch(Ref("greenhouse", "anthropic"), client, options=OPTIONS)
    assert depts.call_count == 0


@respx.mock
async def test_departments_call_goes_through_the_same_host_bucket(fixture):
    """Both requests hit `boards-api.greenhouse.io`, so the second waits on the same
    2 rps token bucket `core.http` keeps per host: the extra request is paid in politeness,
    not in a parallel burst."""
    sleeps: list[float] = []
    clock = [0.0]

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay

    client = make_client(timeout_secs=5, sleep=fake_sleep, clock=lambda: clock[0])
    full = fixture("greenhouse", "anthropic.json")
    respx.get(BOARD_URL).mock(return_value=httpx.Response(200, json=_list_only_board(full)))
    respx.get(DEPARTMENTS_URL).mock(
        return_value=httpx.Response(200, json=fixture("greenhouse", "anthropic_departments.json"))
    )

    await fetch(Ref("greenhouse", "anthropic"), client, options=LIST_ONLY)

    bucket = client._buckets["boards-api.greenhouse.io"]
    assert len(client._buckets) == 1
    # Burst capacity is 2 at 2 rps, so both requests spent a token and neither slept; a
    # third would have. The clock is frozen, so no refill hides the spend.
    assert bucket._tokens == 0.0 and sleeps == []
