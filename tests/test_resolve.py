"""§5.11 resolution: the ~30-URL mixed-case table, plus the directory read path (§6.6).

"Six lines of regex decide whether a paid run works at all" — and the failure mode that
matters is not a clean miss but a *truncated* capture that 404s under the user's own
slug, which is what the `OpenAI` and `Palantir` rows below pin down.
"""

from __future__ import annotations

import gzip
import json

import httpx
import pytest
import respx

from core.directory import Directory, load_directory, parse_jsonl
from core.http import make_client
from core.models import Ref
from core.resolve import Unresolved, needs_directory, resolve

# (input, provider, slug, region)
URL_TABLE = [
    ("https://job-boards.greenhouse.io/anthropic", "greenhouse", "anthropic", None),
    ("https://boards.greenhouse.io/stripe", "greenhouse", "stripe", None),
    ("http://boards.greenhouse.io/figma/jobs/4567", "greenhouse", "figma", None),
    ("boards.greenhouse.io/airbnb", "greenhouse", "airbnb", None),
    (
        "https://boards.greenhouse.io/embed/job_board?for=discord",
        "greenhouse",
        "discord",
        None,
    ),
    ("https://job-boards.greenhouse.io/Anthropic", "greenhouse", "Anthropic", None),
    ("https://job-boards.greenhouse.io/anthropic?utm=x", "greenhouse", "anthropic", None),
    ("https://job-boards.greenhouse.io/some_co.io", "greenhouse", "some_co.io", None),
    ("https://jobs.lever.co/palantir", "lever", "palantir", None),
    ("https://jobs.lever.co/Palantir", "lever", "Palantir", None),
    ("https://jobs.lever.co/palantir/1234-5678", "lever", "palantir", None),
    ("https://jobs.eu.lever.co/lever", "lever", "lever", "eu"),
    ("https://jobs.eu.lever.co/BackMarket/abc", "lever", "BackMarket", "eu"),
    ("https://jobs.ashbyhq.com/openai", "ashby", "openai", None),
    ("https://jobs.ashbyhq.com/OpenAI", "ashby", "OpenAI", None),
    ("https://jobs.ashbyhq.com/OPENAI/1234", "ashby", "OPENAI", None),
    ("jobs.ashbyhq.com/Ramp#anything", "ashby", "Ramp", None),
    ("https://acme.recruitee.com", "recruitee", "acme", None),
    ("https://Acme-Corp.recruitee.com/o/engineer", "recruitee", "Acme-Corp", None),
    ("http://acme.recruitee.com/", "recruitee", "acme", None),
    ("https://ats.rippling.com/acme/jobs", "rippling", "acme", None),
    ("https://ats.rippling.com/AcmeCo", "rippling", "AcmeCo", None),
    ("https://acme.jobs.personio.de", "personio", "acme", None),
    ("https://acme.jobs.personio.com/job/1", "personio", "acme", None),
    ("https://Personio.jobs.personio.de/xml", "personio", "Personio", None),
    ("greenhouse:anthropic", "greenhouse", "anthropic", None),
    ("lever:palantir", "lever", "palantir", None),
    ("ashby:OpenAI", "ashby", "OpenAI", None),
    ("Recruitee:acme", "recruitee", "acme", None),
    ("personio: personio", "personio", "personio", None),
    ("rippling:acme", "rippling", "acme", None),
]


@pytest.mark.parametrize(("entry", "provider", "slug", "region"), URL_TABLE)
def test_url_and_prefix_table(entry: str, provider: str, slug: str, region: str | None):
    ref = resolve(entry)
    assert isinstance(ref, Ref), ref
    assert (ref.provider, ref.slug, ref.region) == (provider, slug, region)
    assert ref.input == entry


def test_lever_casing_is_never_folded():
    """`Palantir` 404s where `palantir` works — the slug must survive verbatim (§5.11)."""
    assert resolve("https://jobs.lever.co/Palantir").slug == "Palantir"
    assert resolve("lever:Palantir").slug == "Palantir"


def test_workday_prefix_carries_the_site():
    ref = resolve("workday:nvidia/NVIDIAExternalCareerSite")
    assert isinstance(ref, Ref)
    assert (ref.provider, ref.slug, ref.site) == ("workday", "nvidia", "NVIDIAExternalCareerSite")


@pytest.mark.parametrize(
    "entry",
    [
        "https://boards.greenhouse.io/embed",
        "https://jobs.ashbyhq.com/api",
        "https://jobs.lever.co/www",
        "https://ats.rippling.com/robots",
    ],
)
def test_reserved_slugs_are_rejected(entry: str):
    assert isinstance(resolve(entry), Unresolved)


def test_percent_encoded_slugs_are_rejected():
    assert isinstance(resolve("greenhouse:acme%2Fx"), Unresolved)


def test_unknown_prefix_is_not_a_provider():
    result = resolve("smartrecruiters:acme")
    assert isinstance(result, Unresolved)


def test_needs_directory_only_for_bare_tokens_and_domains():
    assert not needs_directory("https://job-boards.greenhouse.io/anthropic")
    assert not needs_directory("lever:palantir")
    assert needs_directory("anthropic")
    assert needs_directory("anthropic.com")
    assert needs_directory("https://anthropic.com/careers")


# --- directory-backed resolution (§5.11 rules 3-4, §6.4) ---

ROWS = [
    {
        "provider": "greenhouse",
        "slug": "anthropic",
        "name_norm": "anthropic",
        "domain": "anthropic.com",
        "status": "ok",
    },
    {
        "provider": "lever",
        "slug": "Palantir",
        "name_norm": "palantir",
        "domain": "palantir.com",
        "status": "ok",
    },
    {"provider": "greenhouse", "slug": "acme", "name_norm": "acme", "status": "ok"},
    {"provider": "lever", "slug": "acme", "name_norm": "acme corp", "status": "ok"},
    {
        "provider": "personio",
        "slug": "deadco",
        "name_norm": "deadco",
        "domain": "deadco.de",
        "status": "dead",
    },
]


@pytest.fixture
def directory() -> Directory:
    return Directory(ROWS, source="test")


def test_bare_token_resolves_through_the_directory(directory: Directory):
    ref = resolve("anthropic", directory=directory)
    assert isinstance(ref, Ref)
    assert (ref.provider, ref.slug, ref.input) == ("greenhouse", "anthropic", "anthropic")


def test_bare_token_matches_name_norm_and_keeps_validated_casing(directory: Directory):
    ref = resolve("Palantir", directory=directory)
    assert isinstance(ref, Ref)
    assert (ref.provider, ref.slug) == ("lever", "Palantir")


def test_ambiguous_token_lists_candidates_and_never_guesses(directory: Directory):
    result = resolve("acme", directory=directory)
    assert isinstance(result, Unresolved)
    assert result.status == "unconfirmed"
    assert result.candidates == ["greenhouse:acme", "lever:acme"]


def test_providers_narrow_an_ambiguous_token(directory: Directory):
    ref = resolve("acme", providers=["lever"], directory=directory)
    assert isinstance(ref, Ref)
    assert (ref.provider, ref.slug) == ("lever", "acme")


def test_domain_lookup(directory: Directory):
    ref = resolve("anthropic.com", directory=directory)
    assert isinstance(ref, Ref)
    assert ref.provider == "greenhouse"


def test_career_site_url_falls_back_to_its_domain(directory: Directory):
    ref = resolve("https://www.anthropic.com/careers", directory=directory)
    assert isinstance(ref, Ref)
    assert ref.provider == "greenhouse"


def test_unknown_domain_says_what_to_do(directory: Directory):
    result = resolve("nosuchcompany.com", directory=directory)
    assert isinstance(result, Unresolved)
    assert result.status == "unresolved_domain"
    assert "ATS prefix" in result.message


def test_unknown_bare_token_is_an_error_row_not_a_probe(directory: Directory):
    result = resolve("nosuchcompany", directory=directory)
    assert isinstance(result, Unresolved)
    assert result.status == "not_found"


def test_dead_companies_are_not_indexed(directory: Directory):
    assert isinstance(resolve("deadco", directory=directory), Unresolved)
    assert len(directory) == 4


def test_missing_directory_degrades_to_an_error_row():
    result = resolve("anthropic")
    assert isinstance(result, Unresolved)
    assert "directory unavailable" in result.message


# --- directory read path (§6.6) ---

BLOB = gzip.compress("\n".join(json.dumps(row) for row in ROWS).encode())


def fast_client():
    """A client whose backoff and rate-cap waits run on a fake clock (see test_http)."""
    clock = [0.0]

    async def sleep(delay: float) -> None:
        clock[0] += delay

    return make_client(sleep=sleep, clock=lambda: clock[0])


def test_parse_jsonl_reads_gzip_and_skips_corrupt_lines():
    blob = gzip.compress(b'{"provider":"lever","slug":"x"}\nnot json\n\n{"no":"slug"}\n')
    assert parse_jsonl(blob) == [{"provider": "lever", "slug": "x"}]


@respx.mock
async def test_load_directory_prefers_jsdelivr():
    jsdelivr = respx.get(url__regex=r"https://cdn\.jsdelivr\.net/.*").mock(
        return_value=httpx.Response(200, content=BLOB)
    )
    raw = respx.get(url__regex=r"https://raw\.githubusercontent\.com/.*").mock(
        return_value=httpx.Response(200, content=BLOB)
    )
    directory = await load_directory(fast_client())
    assert directory.source == "jsdelivr"
    assert jsdelivr.call_count == 1
    assert raw.call_count == 0
    assert len(directory) == 4


@respx.mock
async def test_load_directory_falls_through_to_raw_then_kv_then_baked(tmp_path):
    respx.get(url__regex=r"https://cdn\.jsdelivr\.net/.*").mock(return_value=httpx.Response(404))
    respx.get(url__regex=r"https://raw\.githubusercontent\.com/.*").mock(
        return_value=httpx.Response(200, content=BLOB)
    )
    directory = await load_directory(fast_client())
    assert directory.source == "raw.githubusercontent"

    respx.get(url__regex=r"https://raw\.githubusercontent\.com/.*").mock(
        return_value=httpx.Response(500)
    )

    class FakeStore:
        async def get_value(self, key: str):
            return BLOB if key == "companies" else None

    async def opener(*, name: str):
        return FakeStore()

    directory = await load_directory(fast_client(), kv_opener=opener)
    assert directory.source == "kv"

    async def empty_opener(*, name: str):
        raise RuntimeError("no store")

    baked = tmp_path / "companies.seed.jsonl.gz"
    baked.write_bytes(BLOB)
    directory = await load_directory(fast_client(), kv_opener=empty_opener, baked_path=baked)
    assert directory.source == "baked"


@respx.mock
async def test_missing_directory_is_graceful(tmp_path):
    respx.get(url__regex=r"https://(cdn|raw)\..*").mock(return_value=httpx.Response(404))

    async def opener(*, name: str):
        raise RuntimeError("no store")

    warnings: list[str] = []
    directory = await load_directory(
        fast_client(),
        kv_opener=opener,
        baked_path=tmp_path / "nothing.gz",
        warnings=warnings,
    )
    assert len(directory) == 0
    assert not directory
    assert warnings == ["directory_unavailable"]
    assert isinstance(resolve("anthropic", directory=directory), Unresolved)


async def test_a_run_of_urls_never_downloads_the_directory():
    """§6.6: the directory is lazy. respx is not even mocked here — any HTTP call fails."""
    entries = ["https://job-boards.greenhouse.io/anthropic", "lever:palantir"]
    assert not any(needs_directory(entry) for entry in entries)
