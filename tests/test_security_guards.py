"""Regression tests for the BUILD V1/V3 security and robustness findings.

One test per finding, each reproducing the reviewer's own PoC:

* V3 S1  — SSRF: a `companies` entry reaching any host and port
* V3 S2  — decompression bomb in the company directory
* V3 S3  — decompression bomb in any HTTP response body
* V3 S5  — `Retry-After: inf`
* V3 S6  — Lever pagination that never terminates
* V3 S12 — a career-site host read out of a query string
* V1 B1 / V3 S4 — `includeRawJson` re-exporting what `redactContacts` removed
"""

from __future__ import annotations

import gzip
import io

import httpx
import pytest
import respx

from core import directory as directory_mod
from core.directory import MAX_DIRECTORY_BYTES, parse_jsonl
from core.http import (
    MAX_RESPONSE_BYTES,
    HttpError,
    ParseError,
    check_host,
    make_client,
    retry_after_seconds,
)
from core.models import Ref
from core.normalize.record import build_job_record
from core.normalize.redact import strip_contact_fields
from core.providers import lever
from core.resolve import parse_prefix, parse_url, resolve, valid_slug

REF = Ref(provider="greenhouse", slug="acme")


@pytest.fixture
def client():
    """Rate limiting on a fake clock, so the 1 rps Lever bucket costs no wall time."""
    clock = [0.0]

    async def sleep(seconds: float) -> None:
        clock[0] += seconds

    return make_client(timeout_secs=5, sleep=sleep, clock=lambda: clock[0])


# --- V3 S1: SSRF ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "recruitee:169.254.169.254?",
        "recruitee:localhost:6379?",
        "personio:127.0.0.1:8080#",
        "greenhouse:acme@evil.example",
        "lever:acme/../../etc",
        "greenhouse:..",
        "recruitee:acme%2e%2e",
    ],
)
def test_a_slug_can_never_terminate_the_url_authority(entry):
    """V3 S1: `?`, `#`, `:` and `@` used to survive into the hostname."""
    assert parse_prefix(entry) is None
    result = resolve(entry)
    assert getattr(result, "status", None) is not None, f"{entry} still resolved to a Ref"


def test_valid_slug_is_a_positive_charset():
    assert valid_slug("acme") and valid_slug("acme-eu_1.2")
    assert not valid_slug("") and not valid_slug("-acme") and not valid_slug("api")
    assert not valid_slug("a" * 101)


def test_client_refuses_a_host_that_is_not_an_ats_or_directory_host():
    """The sink guard, so a future call site cannot re-open S1."""
    check_host("https://boards-api.greenhouse.io/v1/boards/acme/jobs")
    check_host("https://cdn.jsdelivr.net/gh/o/ats-directory@main/companies.jsonl.gz")
    for bad in (
        "https://169.254.169.254/latest/meta-data/",
        "https://localhost:6379/",
        "http://boards-api.greenhouse.io/v1/boards/acme/jobs",  # plaintext
        "https://greenhouse.io.evil.example/",
    ):
        with pytest.raises(HttpError):
            check_host(bad)


async def test_client_get_refuses_a_non_ats_host(client):
    with pytest.raises(HttpError):
        await client.get("https://169.254.169.254/latest/meta-data/")


def test_directory_rows_with_an_unusable_slug_are_skipped():
    """A poisoned directory row is the same vector as a poisoned input (V3 S1)."""
    rows = [
        {"provider": "recruitee", "slug": "169.254.169.254?"},
        {"provider": "recruitee", "slug": "bunq"},
    ]
    found = directory_mod.Directory(rows).lookup("bunq")
    assert [ref.slug for ref in found] == ["bunq"]
    assert directory_mod.Directory(rows).lookup("169.254.169.254?") == []


# --- V3 S12: host read out of a query string -----------------------------------------


def test_a_career_site_host_is_only_read_from_the_host_position():
    assert parse_url("https://attacker.example/?x=jobs.lever.co/palantir") is None
    assert parse_url("https://jobs.lever.co/palantir").slug == "palantir"
    assert parse_url("jobs.lever.co/palantir").slug == "palantir"
    assert parse_url("https://bunq.recruitee.com").slug == "bunq"


# --- V3 S2 / S3: decompression bombs --------------------------------------------------


SMALL_LIMIT = 64 * 1024


def _bomb(size: int) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as handle:
        handle.write(b"\0" * size)
    return buffer.getvalue()


def test_directory_gzip_is_capped(monkeypatch):
    """V3 S2: 102 kB in, 100 MB out, 1 GB peak RSS — OOM at every memory tier."""
    assert MAX_DIRECTORY_BYTES == 64 * 1024 * 1024
    monkeypatch.setattr(directory_mod, "MAX_DIRECTORY_BYTES", SMALL_LIMIT)
    blob = _bomb(SMALL_LIMIT * 64)
    assert len(blob) < 20_000, "precondition: the bomb is small on the wire"
    with pytest.raises(ValueError):
        parse_jsonl(blob)
    # A directory that does not fit is a missing directory, never a failed run.
    assert directory_mod._rows(blob) == []
    assert parse_jsonl(gzip.compress(b'{"provider": "lever", "slug": "acme"}'))


@respx.mock
async def test_response_body_is_capped(client, monkeypatch):
    """V3 S3: httpx inflates `Accept-Encoding: gzip` into `.content` with no limit."""
    assert MAX_RESPONSE_BYTES == 64 * 1024 * 1024
    monkeypatch.setattr("core.http.MAX_RESPONSE_BYTES", SMALL_LIMIT)
    url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            content=_bomb(SMALL_LIMIT * 64),
        )
    )
    with pytest.raises(ParseError):
        await client.get(url)


@respx.mock
async def test_a_normal_body_still_decodes(client):
    """The cap must not change the ordinary path: gzip in, parsed JSON out."""
    url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    payload = gzip.compress(b'{"jobs": [{"id": 1}]}')
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            content=payload,
        )
    )
    assert await client.get_json(url) == {"jobs": [{"id": 1}]}


# --- V3 S5: Retry-After ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [("30", 30.0), ("120", 60.0), ("99999999", 60.0), ("inf", None), ("1e309", None), ("-5", 0.0)],
)
def test_retry_after_is_clamped(header, expected):
    response = httpx.Response(429, headers={"retry-after": header})
    assert retry_after_seconds(response) == expected


# --- V3 S6: Lever pagination ----------------------------------------------------------


@respx.mock
async def test_lever_pagination_terminates(client):
    """A host answering every page exactly full used to loop forever."""
    page = [{"id": f"job-{n}", "text": "x"} for n in range(lever.PAGE_SIZE)]
    route = respx.get(url__startswith="https://api.lever.co/v0/postings/loop").mock(
        return_value=httpx.Response(200, json=page)
    )
    with pytest.raises(ParseError):
        await lever._postings_for(client, "api.lever.co", "loop")
    assert route.call_count == lever.MAX_PAGES


# --- V1 B1 / V3 S4: raw must not re-export what redaction removed ---------------------


def test_raw_never_carries_contact_shaped_keys_or_ad_bodies():
    job = {
        "id": 1,
        "title": "Engineer",
        "mailbox_email": "job.2dvvr@vandebron.recruitee.com",
        "recruiter": {"name": "Anna", "phone": "+31 6 12345678"},
        "activeJobApplication": {"creator": {"email": "x@y.nl"}},
        "hiringManager": "Anna Jansen",
        "content": "<p>Bel Anna op +31 6 12345678 of mail anna.jansen@vandebron.nl</p>",
        "questions": [{"label": "Notes", "value": "reach me at anna.jansen@vandebron.nl"}],
    }
    record = build_job_record(
        REF, {"job": job, "sourceId": "1", "title": "Engineer"}, {"includeRawJson": True}
    )
    blob = str(record.raw)
    assert "@" not in blob and "12345678" not in blob, blob
    assert record.raw["id"] == 1 and record.raw["title"] == "Engineer"
    for gone in ("mailbox_email", "recruiter", "activeJobApplication", "hiringManager", "content"):
        assert gone not in record.raw
    assert record.raw["questions"] == [{"label": "Notes", "value": "reach me at [redacted]"}]


def test_raw_is_null_unless_asked_for():
    record = build_job_record(REF, {"job": {"id": 1}, "sourceId": "1", "title": "T"}, {})
    assert record.raw is None


def test_strip_contact_fields_is_depth_bounded():
    deep = current = {}
    for _ in range(40):
        current["next"] = current = {}
    assert strip_contact_fields(deep) is not None  # terminates, does not recurse forever
