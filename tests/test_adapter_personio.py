"""Personio adapter (SPEC v2 §5.8, §10.1). Fixtures are real captures, no network.

``sample.xml``    — `https://personio.jobs.personio.de/xml?language=en`, the §4.1 prefill
                    board and the §10.2 contract slug; carries the verified
                    `seniority`/`yearsOfExperience`/`createdAt` values.
``orderbird.xml`` — a second live board: 5 positions, `additionalOffices` with 13 entries,
                    a `working_student` + `part-time` refinement, and one position whose
                    `<jobDescriptions/>` is genuinely empty.
``malformed.xml`` — the first 6,000 bytes of that same response, i.e. a truncated body.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from core.http import Client, NotFound, ParseError, make_client
from core.models import Ref
from core.providers import get_adapter
from core.providers.personio import SPEC, fetch, job_url, parse_feed, to_record
from tests.conftest import load_fixture

OPTIONS = {
    "includeDescription": True,
    "descriptionFormat": "both",
    "redactContacts": True,
    "scrapedAt": "2026-08-25T00:00:00Z",
}

SAMPLE_REF = Ref("personio", "personio", input="personio:personio")
ORDERBIRD_REF = Ref("personio", "orderbird", input="orderbird.jobs.personio.de")


def records(name: str, ref: Ref, options: dict | None = OPTIONS):
    positions = parse_feed(load_fixture("personio", name))
    return [to_record(position, ref, options) for position in positions]


@pytest.fixture
def client() -> Client:
    """Instant backoff and instant rate cap — the schedule is `test_http.py`'s subject."""
    clock = [0.0]

    async def fake_sleep(delay: float) -> None:
        clock[0] += delay

    return make_client(timeout_secs=5, sleep=fake_sleep, clock=lambda: clock[0])


# --------------------------------------------------------------------------- §10.1 core


def test_first_record_exact_values():
    """The §10.1 assertion list, against the board §4.1 ships as the prefill."""
    row = records("sample.xml", SAMPLE_REF)[0]

    assert row.id == "personio:personio:1834171"
    assert row.title == "Staff Software Engineer, Data Platform"
    assert row.city == "Munich"
    # §4.5.1 step 5: `office` is bare free text, and "Munich" alone never guesses DE.
    assert row.countryCode is None
    # No ATS remote flag, no marker in the location, none in the title, and the §4.5.2
    # rank-5 patterns do not fire on "This position is hybrid based in either …".
    assert row.remote is None
    assert row.remoteSource is None
    assert (row.employmentType, row.employmentTypeSource) == ("full_time", "ats")
    assert row.employmentTypeRaw == "permanent"
    # Personio publishes no structured pay and the description carries no parsable range.
    assert (row.salaryMin, row.salaryMax) == (None, None)
    assert (row.salaryCurrency, row.salaryInterval, row.salarySource) == (None, None, None)
    assert row.postedAt == "2024-11-13T14:10:41Z"
    assert row.postedAtSource == "createdAt"
    assert row.url == "https://personio.jobs.personio.de/job/1834171"


def test_seniority_and_years_of_experience_are_provider_passthrough():
    """§5.8 / T-M7: copied verbatim from Personio's own vocabulary, never inferred."""
    row = records("sample.xml", SAMPLE_REF)[0]
    assert (row.seniority, row.yearsOfExperience) == ("experienced", "7-10")
    assert row.recordType == "job"
    assert (row.provider, row.companySlug, row.sourceId) == ("personio", "personio", "1834171")
    assert row.input == "personio:personio"


def test_company_department_and_scraped_at():
    row = records("sample.xml", SAMPLE_REF)[0]
    assert row.company == "Personio SE & Co. KG"  # `subcompany`, entities unescaped
    assert row.department == "Product and Tech"
    assert row.team is None  # Personio publishes no second level
    assert row.scrapedAt == "2026-08-25T00:00:00Z"


def test_additional_offices_become_sorted_locations():
    """§5.8: `office` + `additionalOffices/office[]`, emitted sorted (§4.5.1 step 8)."""
    row = records("sample.xml", SAMPLE_REF)[0]
    assert row.locationRaw == "Munich"
    assert [loc.raw for loc in row.locations] == ["Berlin", "Munich"]


def test_description_sections_are_joined_under_h3_headers():
    row = records("sample.xml", SAMPLE_REF)[0]
    assert row.descriptionHtml is not None
    assert row.descriptionHtml.startswith(
        "<h3>The Role: How you'll make an impact at Personio</h3>"
    )
    assert row.descriptionHtml.count("<h3>") == 4
    assert "Data Platform team is on a mission" in row.descriptionText
    assert row.descriptionRedacted is False


def test_description_is_dropped_unless_requested():
    row = records("sample.xml", SAMPLE_REF, {"scrapedAt": "2026-08-25T00:00:00Z"})[0]
    assert row.descriptionHtml is None
    assert row.descriptionText is None
    assert row.descriptionRedacted is None
    assert row.title == "Staff Software Engineer, Data Platform"


def test_raw_is_only_emitted_on_request():
    assert records("sample.xml", SAMPLE_REF)[0].raw is None
    row = records("sample.xml", SAMPLE_REF, {**OPTIONS, "includeRawJson": True})[0]
    # `occupation` / `occupationCategory` stay unmapped but reachable (§5.8).
    assert row.raw["occupation"] == "software_and_web_development"
    assert row.raw["occupationCategory"] == "it_software"


# ------------------------------------------------------------------- second live board


def test_orderbird_board_maps_every_position():
    rows = records("orderbird.xml", ORDERBIRD_REF)
    assert len(rows) == 5
    assert len({row.id for row in rows}) == 5
    assert all(row.company == "orderbird GmbH" for row in rows)
    assert all(row.provider == "personio" for row in rows)

    first = rows[0]
    assert first.id == "personio:orderbird:1935524"
    assert first.title == "Account Executive - Gastro/Tech/Saas (d/w/m)"
    assert first.city == "Berlin"
    assert first.countryCode is None
    assert first.remote is None
    assert (first.employmentType, first.employmentTypeSource) == ("full_time", "ats")
    assert (first.salaryMin, first.salaryMax, first.salarySource) == (None, None, None)
    assert first.postedAt == "2025-01-27T11:06:05Z"
    assert first.url == "https://orderbird.jobs.personio.de/job/1935524"


def test_orderbird_office_list_is_sorted_and_country_resolved():
    """14 offices, sorted by `(countryCode, region, city, raw)`; "Deutschland" resolves."""
    first = records("orderbird.xml", ORDERBIRD_REF)[0]
    raws = [loc.raw for loc in first.locations]
    assert len(raws) == 14
    assert raws[:3] == ["Berlin", "Brandenburg an der Havel", "Eschborn"]
    # Country-bearing entries sort last because their `countryCode` is non-empty.
    germany = first.locations[-1]
    assert (germany.raw, germany.country, germany.countryCode) == ("Deutschland", "Germany", "DE")
    assert germany.city is None


def test_schedule_refines_working_student_to_part_time():
    """§4.5.4: `employmentType: working_student` + `schedule: part-time` -> part_time.

    The live feed spells it `working_student`; §5.8's table writes `working-student`. The
    canonical key strips everything but `[a-z]`, so both land on the same entry.
    """
    werkstudent = records("orderbird.xml", ORDERBIRD_REF)[4]
    assert werkstudent.title == "Werkstudent People & Culture (d/w/m)"
    assert (werkstudent.employmentType, werkstudent.employmentTypeSource) == ("part_time", "ats")
    assert werkstudent.employmentTypeRaw == "working_student"
    assert (werkstudent.seniority, werkstudent.yearsOfExperience) == ("entry-level", "lt-1")


def test_position_without_descriptions_still_emits():
    """A real position whose `<jobDescriptions/>` is empty: nulls, no exception."""
    werkstudent = records("orderbird.xml", ORDERBIRD_REF)[4]
    assert werkstudent.descriptionHtml is None
    assert werkstudent.descriptionText is None
    assert werkstudent.descriptionRedacted is None
    assert werkstudent.url == "https://orderbird.jobs.personio.de/job/1714247"


def test_department_falls_back_to_recruiting_category():
    positions = parse_feed(load_fixture("personio", "orderbird.xml"))
    positions[0].pop("department")
    row = to_record(positions[0], ORDERBIRD_REF, OPTIONS)
    assert row.department == "002_Sales & Growth"


# ------------------------------------------------------------------- failure semantics


def test_malformed_xml_is_parse_error_not_a_crash():
    """§5.8: truncated / non-XML body -> `parse_error` (§5.12), never a traceback."""
    with pytest.raises(ParseError):
        parse_feed(load_fixture("personio", "malformed.xml"))


def test_html_body_with_status_200_is_parse_error():
    with pytest.raises(ParseError):
        parse_feed("<html><body>Personio</body></html>")


def test_entity_expansion_is_refused():
    """§5.8: defusedxml, so a billion-laughs feed is a `parse_error`, not 3 GB of RAM."""
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE workzag-jobs ['
        '<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        "]><workzag-jobs><position><name>&b;</name></position></workzag-jobs>"
    )
    with pytest.raises(ParseError):
        parse_feed(bomb)


def test_empty_feed_is_not_an_error():
    assert parse_feed("<workzag-jobs></workzag-jobs>") == []


# ---------------------------------------------------------- §10.1 empty-objects contract


EMPTY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
<position>
    <id></id>
    <subcompany></subcompany>
    <office></office>
    <additionalOffices></additionalOffices>
    <department></department>
    <recruitingCategory></recruitingCategory>
    <name></name>
    <jobDescriptions></jobDescriptions>
    <employmentType></employmentType>
    <seniority></seniority>
    <schedule></schedule>
    <yearsOfExperience></yearsOfExperience>
    <createdAt></createdAt>
</position>
<position>
</position>
</workzag-jobs>
"""


def test_empty_objects_produce_all_null_and_never_raise():
    """§10.1 `test_adapters_empty_objects`: every nested object empty or absent.

    Both shapes go through the same `.get()` chains — an empty element and a missing one
    are the same null. `company` is the one field that is not null, because §5.8 makes the
    slug its documented fallback.
    """
    rows = [to_record(position, SAMPLE_REF, OPTIONS) for position in parse_feed(EMPTY_XML)]
    assert len(rows) == 2
    for row in rows:
        assert row.sourceId is None
        assert row.title is None
        assert row.department is None
        assert (row.locationRaw, row.city, row.region, row.country, row.countryCode) == (
            None,
            None,
            None,
            None,
            None,
        )
        assert row.locations == []
        assert (row.remote, row.workplaceType, row.remoteSource) == (None, None, None)
        assert (row.employmentType, row.employmentTypeRaw, row.employmentTypeSource) == (
            None,
            None,
            None,
        )
        assert (row.seniority, row.yearsOfExperience) == (None, None)
        assert (row.salaryMin, row.salaryMax, row.salaryCurrency) == (None, None, None)
        assert (row.salaryInterval, row.salarySource, row.salaryRaw) == (None, None, None)
        assert (row.postedAt, row.postedAtSource, row.updatedAt) == (None, None, None)
        assert (row.descriptionHtml, row.descriptionText, row.descriptionRedacted) == (
            None,
            None,
            None,
        )
        # No id means no link: a fabricated `/job/` URL would 404 on the buyer (§5.8).
        assert row.url is None
        assert row.company == "personio"
        assert row.id == "personio:personio:"


def test_to_record_survives_an_entirely_absent_position():
    row = to_record({}, SAMPLE_REF, OPTIONS)
    assert (row.title, row.url, row.postedAt) == (None, None, None)


# ------------------------------------------------------------------------ url building


def test_job_url_needs_both_halves_and_keeps_the_answering_host():
    assert job_url("acme", "42") == "https://acme.jobs.personio.de/job/42"
    assert job_url("acme", "42", "com") == "https://acme.jobs.personio.com/job/42"
    assert job_url("acme", None) is None
    assert job_url(None, "42") is None


# ------------------------------------------------------------------------------ fetch


DE_URL = "https://personio.jobs.personio.de/xml"
COM_URL = "https://personio.jobs.personio.com/xml"


@respx.mock
async def test_fetch_reads_the_de_host_with_language_en(client: Client):
    route = respx.get(DE_URL).mock(
        return_value=httpx.Response(200, text=load_fixture("personio", "sample.xml"))
    )
    rows = await fetch(SAMPLE_REF, client, OPTIONS)
    assert [row.id for row in rows] == ["personio:personio:1834171"]
    assert route.calls[0].request.url.params["language"] == "en"


@respx.mock
async def test_fetch_falls_back_to_the_com_host(client: Client):
    """§5.8: `.de` first, `.com` on failure — and the emitted URL follows the host."""
    de = respx.get(DE_URL).mock(return_value=httpx.Response(307, headers={"location": "/"}))
    com = respx.get(COM_URL).mock(
        return_value=httpx.Response(200, text=load_fixture("personio", "sample.xml"))
    )
    rows = await fetch(SAMPLE_REF, client, OPTIONS)
    assert de.call_count == 1 and com.call_count == 1
    assert rows[0].url == "https://personio.jobs.personio.com/job/1834171"


@respx.mock
async def test_fetch_307_on_both_hosts_is_not_found(client: Client):
    """§5.12: 307 is `not_found` with no retry — one call per host, then done."""
    de = respx.get(DE_URL).mock(return_value=httpx.Response(307, headers={"location": "/"}))
    com = respx.get(COM_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await fetch(SAMPLE_REF, client, OPTIONS)
    assert (de.call_count, com.call_count) == (1, 1)


@respx.mock
async def test_fetch_retries_a_truncated_body_once_then_parse_errors(client: Client):
    """§5.12 malformed body: 1 retry, then `parse_error`. Two hosts x two attempts."""
    de = respx.get(DE_URL).mock(
        return_value=httpx.Response(200, text=load_fixture("personio", "malformed.xml"))
    )
    with pytest.raises(ParseError):
        await fetch(SAMPLE_REF, client, OPTIONS)
    assert de.call_count == 2


@respx.mock
async def test_fetch_decodes_utf8_when_the_header_omits_the_charset(client: Client):
    """The body is parsed from bytes, so the XML declaration wins over a bare header."""
    raw = load_fixture("personio", "orderbird.xml").encode("utf-8")
    respx.get("https://orderbird.jobs.personio.de/xml").mock(
        return_value=httpx.Response(200, content=raw, headers={"content-type": "text/xml"})
    )
    rows = await fetch(ORDERBIRD_REF, client, OPTIONS)
    assert [loc.raw for loc in rows[0].locations][7] == "Köln"


# --------------------------------------------------------------------- registry contract


def test_registry_exposes_the_adapter():
    module = get_adapter("personio")
    assert module.SPEC is SPEC
    assert module.fetch is fetch
    assert (SPEC.name, SPEC.host_rate_limit, SPEC.needs_detail_call) == ("personio", 2.0, False)
