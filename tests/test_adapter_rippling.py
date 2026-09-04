"""Rippling adapter tests (SPEC v2 §5.7, §4.5, §10.1).

Fixtures are real payloads captured from `api.rippling.com`, on two boards:

* ``rippling`` — Rippling's own board (``list_dupes.json`` / ``detail.json`` /
  ``detail_dept_tree.json``, refreshed by ``scripts/refresh_fixtures.py``): 25 list rows
  for 13 uuids. Carries the provider-specific edge cases — uuid duplicates merged into
  one record with several locations, ``employmentType.id`` -> ``full_time``, and the
  ``department_tree`` hierarchy that v1 read a non-existent ``label`` key for (V2 T-C2,
  T-C3). Only the two uuids with a committed detail payload are asserted on; the rest
  exercise the §5.12 ``detail_failed`` path for free.
* ``atlas-data-storage`` — captured 2026-08-26, five singleton jobs, every one with
  ``payRangeDetails``. Rippling's own board publishes no pay ranges, so this is the board
  the §10.1 "exact expected values for the first record" assertions run against. List
  truncated to the first five uuids; both files are well under the fixture size cap.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from core.http import HttpError, NotFound, ParseError, make_client
from core.models import Ref
from core.normalize.html import sanitize_html
from core.providers import get_adapter
from core.providers import rippling as adapter

BASE = "/platform/api/ats/v1/board/{slug}/jobs"


class _Clock:
    """Virtual time, so the real 2 rps token bucket runs without real seconds passing.
    Sleeping advances the clock instead of blocking; nothing here fakes the rate itself."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _ref(slug: str) -> Ref:
    return Ref(provider="rippling", slug=slug)


def _by_uuid(records) -> dict[str, Any]:
    return {record.sourceId: record for record in records}


def _board(fixture, slug: str) -> tuple[list[dict], dict[str, dict]]:
    """``(list rows, {uuid: detail})`` for one board."""
    if slug == "rippling":
        details = [fixture("rippling", "detail.json"), fixture("rippling", "detail_dept_tree.json")]
        return fixture("rippling", "list_dupes.json"), {d["uuid"]: d for d in details}
    return fixture("rippling", f"{slug}.json"), fixture("rippling", f"{slug}-details.json")


def _client(
    slug: str,
    rows: Any,
    details: dict[str, Any],
    *,
    calls: list[str] | None = None,
    list_status: int = 200,
    list_body: str | None = None,
    detail_status: int = 200,
):
    """A `core.http.Client` serving one board out of memory."""
    base = BASE.format(slug=slug)

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if calls is not None:
            calls.append(path)
        if path == base:
            if list_body is not None:
                return httpx.Response(list_status, content=list_body)
            return httpx.Response(list_status, json=rows)
        uuid = path.rsplit("/", 1)[-1]
        if path.startswith(base + "/") and uuid in details:
            return httpx.Response(detail_status, json=details[uuid])
        return httpx.Response(404, json={"error": "Not Found"})

    clock = _Clock()
    return make_client(transport=httpx.MockTransport(handle), sleep=clock.sleep, clock=clock)


async def _fetch(slug: str, rows: Any, details: dict[str, Any], **kwargs: Any):
    options = kwargs.pop("options", None)
    client = _client(slug, rows, details, **kwargs)
    try:
        return await adapter.fetch(_ref(slug), client, options)
    finally:
        await client.aclose()


# --- registry contract ---------------------------------------------------------------------


def test_registry_exposes_the_adapter():
    module = get_adapter("rippling")
    assert module is adapter
    assert adapter.SPEC.name == "rippling"
    assert adapter.SPEC.needs_detail_call is True
    assert adapter.SPEC.host_rate_limit == 0.16  # Rippling documents 100 req/10 min


# --- §10.1 exact expected values for the first record ---------------------------------------


async def test_first_record_exact_values(fixture):
    rows, details = _board(fixture, "atlas-data-storage")
    records = await _fetch("atlas-data-storage", rows, details)

    assert len(records) == 5
    first = records[0]
    assert first.id == "rippling:atlas-data-storage:787eb126-aeb3-4077-a1ab-ee76ebdb8bab"
    assert first.sourceId == "787eb126-aeb3-4077-a1ab-ee76ebdb8bab"
    assert first.title == "Financial Controller"
    assert first.company == "AtlasBase"
    assert first.city == "South San Francisco"
    assert first.region == "CA"
    assert first.countryCode == "US"
    assert first.remote is None  # no ATS flag, no marker in the text — never guessed
    assert first.employmentType == "full_time"
    assert first.employmentTypeSource == "ats"
    assert first.salaryMin == 230000.0
    assert first.salaryMax == 260000.0
    assert first.salaryCurrency == "USD"
    assert first.salaryInterval == "year"
    assert first.salarySource == "ats"
    assert first.postedAt == "2025-11-20T03:31:54Z"
    assert first.postedAtSource == "createdOn"
    assert first.url == (
        "https://ats.rippling.com/atlas-data-storage/jobs/787eb126-aeb3-4077-a1ab-ee76ebdb8bab"
    )
    assert first.warnings == []


async def test_pay_range_details_map_to_every_salary_field(fixture):
    """`payRangeDetails[0]` is `{rangeStart, rangeEnd, currency, frequency}` — the whole
    band, not just the currency, has to land (§5.7, §4.5.3)."""
    rows, details = _board(fixture, "atlas-data-storage")
    records = await _fetch("atlas-data-storage", rows, details)

    assert [(r.salaryMin, r.salaryMax) for r in records] == [
        (230000.0, 260000.0),
        (190000.0, 225000.0),
        (110000.0, 135000.0),
        (80000.0, 115000.0),
        (75000.0, 110000.0),
    ]
    assert {r.salaryCurrency for r in records} == {"USD"}
    assert {r.salaryInterval for r in records} == {"year"}
    assert {r.salarySource for r in records} == {"ats"}


# --- §10.1 provider-specific edge cases -----------------------------------------------------


async def test_uuid_duplicates_merge_into_one_record_with_several_locations(fixture):
    """The list endpoint returns one row per (job x location); 25 rows are 13 jobs."""
    rows, details = _board(fixture, "rippling")
    calls: list[str] = []
    records = await _fetch("rippling", rows, details, calls=calls)

    assert len(rows) == 25
    assert len(records) == 13
    assert len({r.id for r in records}) == 13

    first = records[0]
    assert first.sourceId == "2f0674e6-f01f-4ecd-b459-e947241c211f"
    assert sum(r["uuid"] == first.sourceId for r in rows) == 4
    # The four list rows and the four detail `workLocations` are one merged, sorted set.
    assert [loc.raw for loc in first.locations] == [
        "Remote (Connecticut, US)",
        "Remote (Massachusetts, US)",
        "Remote (New Jersey, US)",
        "New York, NY",
    ]
    assert first.locationRaw == "Remote (Connecticut, US)"
    assert first.countryCode == "US"
    assert first.remote is True
    assert first.remoteSource == "location"

    # A second uuid whose two list rows are its only source of locations.
    pittsburgh = _by_uuid(records)["94486f41-6474-446a-b67a-c164e11354ea"]
    assert [loc.raw for loc in pittsburgh.locations] == ["Cleveland, OH", "Pittsburgh, PA"]

    # Grouping happens before the detail calls: 1 list + 13 details, never 1 + 25.
    assert len(calls) == 14
    # Only two uuids have a committed detail payload; the other eleven take the §5.12
    # detail-failure path, and every one of them is still emitted.
    assert {r.sourceId for r in records if not r.warnings} == set(details)
    assert [r.warnings for r in records if r.sourceId not in details] == [["detail_failed"]] * 11


async def test_locations_survive_a_reordered_list(fixture):
    """Same job, list rows shuffled -> identical locations[] and identical changeHash.
    This is §4.5.6's phantom-change guard seen from the adapter side (V2 T-H5)."""
    rows, details = _board(fixture, "rippling")
    forward = await _fetch("rippling", rows, details)
    reversed_ = await _fetch("rippling", list(reversed(rows)), details)

    by_id = {r.id: r for r in reversed_}
    for record in forward:
        other = by_id[record.id]
        assert [loc.raw for loc in record.locations] == [loc.raw for loc in other.locations]
        assert record.locationRaw == other.locationRaw
        assert record.changeHash == other.changeHash


async def test_employment_type_reads_id_not_label(fixture):
    """`{"label": "SALARIED_FT", "id": "Salaried, full-time"}` — `.label` is the machine
    token and `.id` the human string, inverted from every other provider (V2 T-C2).
    Reading `.label` would make every Rippling job `other`."""
    rows, details = _board(fixture, "rippling")
    records = await _fetch("rippling", rows, details)

    for uuid, detail in details.items():
        assert detail["employmentType"] == {"label": "SALARIED_FT", "id": "Salaried, full-time"}
        record = _by_uuid(records)[uuid]
        assert record.employmentType == "full_time"
        assert record.employmentTypeRaw == "Salaried, full-time"
        assert record.employmentTypeSource == "ats"


async def test_department_comes_from_the_tree_and_team_from_its_leaf(fixture):
    """Detail `department` is `{name, base_department, department_tree}` with **no
    `label` key**; the list row's is `{id, label}` with no tree (V2 T-C3)."""
    rows, details = _board(fixture, "rippling")
    records = await _fetch("rippling", rows, details)

    by_uuid = _by_uuid(records)
    two_level = "2f0674e6-f01f-4ecd-b459-e947241c211f"
    assert "label" not in details[two_level]["department"]
    assert details[two_level]["department"]["department_tree"] == [
        "Sales",
        "Channel Sales Account Executives",
    ]
    assert by_uuid[two_level].department == "Sales"
    assert by_uuid[two_level].team == "Channel Sales Account Executives"

    # A three-level tree keeps the root as department and the leaf as team.
    three_level = "94ac7084-9b63-4e9f-9e2a-fd5da349172d"
    assert details[three_level]["department"]["department_tree"] == [
        "Sales",
        "Channel Sales Account Executives",
        "Broker",
    ]
    assert (by_uuid[three_level].department, by_uuid[three_level].team) == ("Sales", "Broker")


async def test_list_only_falls_back_to_the_list_department_label(fixture):
    """`outputProfile: "minimal"` skips the detail call entirely (§5.7): one request per
    company, and `employmentType` / `createdOn` / salary are null by design."""
    rows, details = _board(fixture, "rippling")
    calls: list[str] = []
    records = await _fetch(
        "rippling", rows, details, calls=calls, options={"outputProfile": "minimal"}
    )

    assert calls == [BASE.format(slug="rippling")]
    assert len(records) == 13
    assert records[0].department == "Sales"  # the list row's `department.label`
    assert records[0].team is None
    assert records[0].employmentType is None
    assert records[0].postedAt is None
    assert records[0].salaryMin is None
    assert records[0].warnings == []  # skipping detail on purpose is not a failure
    # Grouping still happens, so the row duplication never reaches the dataset.
    assert [loc.raw for loc in records[0].locations] == [
        "Remote (Connecticut, US)",
        "Remote (Massachusetts, US)",
        "Remote (New Jersey, US)",
        "New York, NY",
    ]


async def test_unlisted_jobs_are_dropped(fixture):
    """Defensive guard, same status as Ashby's `isListed` (V2 T-M8) — the sampled board
    has `unlistedFromSearch: false` everywhere, so the `true` case is synthesized."""
    rows, details = _board(fixture, "atlas-data-storage")
    hidden = rows[0]["uuid"]
    details = {**details, hidden: {**details[hidden], "unlistedFromSearch": True}}

    records = await _fetch("atlas-data-storage", rows, details)
    assert hidden not in {r.sourceId for r in records}
    assert len(records) == 4


async def test_raw_json_is_the_provider_payload_not_the_salary_alias(fixture):
    rows, details = _board(fixture, "atlas-data-storage")
    records = await _fetch("atlas-data-storage", rows, details, options={"includeRawJson": True})

    band = records[0].raw["payRangeDetails"][0]
    assert band["rangeStart"] == 230000.0 and band["rangeEnd"] == 260000.0
    assert "min" not in band and "max" not in band


async def test_description_concatenates_the_detail_sections(fixture):
    rows, details = _board(fixture, "atlas-data-storage")
    records = await _fetch(
        "atlas-data-storage",
        rows,
        details,
        options={"includeDescription": True, "descriptionFormat": "both"},
    )

    sections = details[records[0].sourceId]["description"]
    assert list(sections) == ["company", "role"]
    assert records[0].descriptionHtml is not None
    # Compared after `sanitize_html`, because that is what the buyer receives: this live
    # fixture's ad body carries a stray `<meta>` tag, which V3 S23 strips along with the
    # rest of the non-content markup. The prose either side of it must survive intact.
    for html in sections.values():
        assert sanitize_html(html) in records[0].descriptionHtml
    assert len(records[0].descriptionText) > 1000


# --- §5.12 failure semantics ----------------------------------------------------------------


async def test_missing_board_is_not_found(fixture):
    rows, details = _board(fixture, "atlas-data-storage")
    with pytest.raises(NotFound) as excinfo:
        await _fetch("atlas-data-storage", rows, details, list_status=404)
    assert excinfo.value.status == "not_found"


async def test_unprocessable_list_response_is_an_http_error(fixture):
    rows, details = _board(fixture, "atlas-data-storage")
    with pytest.raises(HttpError) as excinfo:
        await _fetch("atlas-data-storage", rows, details, list_status=422)
    assert excinfo.value.status == "http_error"
    assert excinfo.value.http_status == 422


@pytest.mark.parametrize("body", ["{not json", json.dumps({"error": "Not Found"})])
async def test_malformed_list_body_is_a_parse_error(fixture, body):
    """Unparseable JSON and a 200 that is not the documented array both end as
    `parse_error`, never as a stack trace and never as an empty board (§5.12)."""
    rows, details = _board(fixture, "atlas-data-storage")
    with pytest.raises(ParseError) as excinfo:
        await _fetch("atlas-data-storage", rows, details, list_body=body)
    assert excinfo.value.status == "parse_error"


async def test_detail_failure_still_emits_the_job(fixture):
    """§5.12: a failed detail call for a single job is a warning, not a lost job — the
    row is delivered (and charged) with description, salary and date null."""
    rows, details = _board(fixture, "atlas-data-storage")
    records = await _fetch("atlas-data-storage", rows, details, detail_status=500)

    assert len(records) == 5
    for record in records:
        assert record.warnings == ["detail_failed"]
        assert record.employmentType is None
        assert record.salaryMin is None and record.salarySource is None
        assert record.postedAt is None and record.postedAtSource is None
        assert record.descriptionHtml is None
    # The list row still carries enough to identify and link the job.
    assert records[0].title == "Financial Controller"
    assert records[0].locationRaw == "South San Francisco, CA"
    assert records[0].url.endswith("/787eb126-aeb3-4077-a1ab-ee76ebdb8bab")


# --- §10.1 test_adapters_empty_objects ------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"uuid": "u1", "name": "Ghost"},
        {
            "uuid": "u1",
            "name": "Ghost",
            "department": {},
            "employmentType": {},
            "workLocation": {},
            "workLocations": [],
            "payRangeDetails": [],
            "description": {},
            "board": {},
        },
        {
            "uuid": "u1",
            "name": "Ghost",
            "department": {"department_tree": []},
            "employmentType": None,
            "workLocation": None,
            "workLocations": None,
            "payRangeDetails": None,
            "description": None,
            "createdOn": None,
        },
    ],
)
def test_empty_objects_yield_nulls_not_exceptions(payload):
    """Every nested provider object empty or absent -> no exception, all-null output.
    This is the test that catches the day Rippling reshapes `department` again."""
    record = adapter.to_record(payload, _ref("acme"))

    assert record.title == (payload.get("name") or None)
    assert record.department is None
    assert record.team is None
    assert record.locationRaw is None
    assert record.city is None
    assert record.region is None
    assert record.country is None
    assert record.countryCode is None
    assert record.locations == []
    assert record.remote is None
    assert record.employmentType is None
    assert record.employmentTypeRaw is None
    assert record.employmentTypeSource is None
    assert record.salaryMin is None
    assert record.salaryMax is None
    assert record.salaryCurrency is None
    assert record.salaryInterval is None
    assert record.salarySource is None
    assert record.postedAt is None
    assert record.postedAtSource is None
    assert record.descriptionHtml is None
    assert record.descriptionText is None
    assert record.url is None
    assert record.company is None


async def test_empty_board_and_junk_rows_yield_no_records():
    """A 200 with an empty array is an empty board, and rows without a `uuid` cannot be
    grouped, charged or linked — both are zero records, not a crash."""
    assert await _fetch("acme", [], {}) == []
    assert await _fetch("acme", [None, "x", {}, {"name": "no uuid"}], {}) == []


# --- V1 H1: a board too big for the budget delivers rows, not a timeout ---------------


async def test_detail_calls_stop_at_the_deadline_and_the_jobs_still_ship(fixture, monkeypatch):
    """The detail call is mandatory (§5.7) and every one queues on the same 2 rps bucket,
    so a 374-uuid board needs ~187 s — past `COMPANY_BUDGET_SECS`. The cancellation used
    to cost the buyer the *entire* company; now the overflow ships list-only with
    `detail_failed`, which §5.12 defines as a delivered row (V1 H1)."""
    rows, details = _board(fixture, "rippling")
    calls: list[str] = []
    client = _client("rippling", rows, details, calls=calls)
    monkeypatch.setattr(adapter, "DETAIL_BATCH", 2)

    # A deadline already in the past: no detail call may be issued at all.
    records = await adapter.fetch(_ref("rippling"), client, {"deadline": -1.0})

    assert len(records) == len({r["uuid"] for r in rows})
    base = BASE.format(slug="rippling")
    assert [call for call in calls if call.startswith(base + "/")] == [], calls
    assert all("detail_failed" in record.warnings for record in records)
    assert all(record.title for record in records), "list-only rows are still real rows"


async def test_without_a_deadline_every_detail_is_fetched(fixture):
    rows, details = _board(fixture, "rippling")
    calls: list[str] = []
    client = _client("rippling", rows, details, calls=calls)
    records = await adapter.fetch(_ref("rippling"), client, {})
    uuids = {r["uuid"] for r in rows}
    assert len([c for c in calls if c.rsplit("/", 1)[-1] in uuids]) == len(uuids)
    assert len(records) == len(uuids)
