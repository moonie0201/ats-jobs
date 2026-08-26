"""Live contract tests — one real request per provider (SPEC v2 §10.2).

These hit the vendors' own public job-board APIs, so they are excluded from CI with
``-m "not live"`` and run on demand::

    pytest -m live

They are the tripwire between fixture refreshes: the unit suite proves we parse the
*captured* payload, this proves the *live* payload still has the shape we parse. A
failure here means the endpoint moved, the slug went dark, or the provider changed its
contract — never that our normalization is wrong.
"""

from __future__ import annotations

import re

import pytest

from core.http import make_client
from core.models import PROVIDERS, Ref
from core.providers import get_adapter

pytestmark = pytest.mark.live

#: One live, non-empty board per provider. Same slugs the committed fixtures came from
#: (``scripts/refresh_fixtures.py``), so a break here and a fixture refresh point at the
#: same payload.
BOARDS: dict[str, str] = {
    "greenhouse": "stripe",
    "lever": "palantir",
    "ashby": "openai",
    "recruitee": "channable",
    "rippling": "rippling",
    "personio": "personio",
}

#: Descriptions are fetched too: for Rippling and Recruitee that is a second live call,
#: and it is the one most likely to break silently.
OPTIONS = {"includeDescription": True, "descriptionFormat": "text", "redactContacts": True}


def test_every_provider_has_a_live_board() -> None:
    """A provider added without a live board would silently skip its contract test."""
    assert set(BOARDS) == set(PROVIDERS)


@pytest.mark.parametrize("provider", sorted(BOARDS))
async def test_live_board_contract(provider: str) -> None:
    ref = Ref(provider=provider, slug=BOARDS[provider], input=f"{provider}:{BOARDS[provider]}")
    async with make_client(timeout_secs=60.0) as client:
        records = await get_adapter(provider).fetch(ref, client, options=OPTIONS)

    assert records, f"{provider}:{ref.slug} returned no jobs — board dark or endpoint moved"

    ids = [r.id for r in records]
    assert all(ids), f"{provider}: some rows have no id"
    assert len(set(ids)) == len(ids), f"{provider}: duplicate ids in one board"

    for record in records:
        assert record.id.startswith(f"{provider}:{ref.slug}:"), record.id
        assert record.provider == provider
        assert record.companySlug == ref.slug
        assert record.title and record.title.strip(), f"{provider}: blank title on {record.id}"
        assert record.url and record.url.startswith("https://"), f"{provider}: bad url {record.url}"
        assert record.sourceId, f"{provider}: no sourceId on {record.id}"
        assert record.contentKey and record.changeHash
        if record.countryCode is not None:
            assert record.countryCode.isupper() and len(record.countryCode) == 2
        if record.salaryMin is not None and record.salaryMax is not None:
            assert record.salaryMin <= record.salaryMax
        # A *structured* salary always names its currency — that is a provider contract,
        # and a silent null there means the compensation object moved. A *parsed* one may
        # legitimately have none: §4.5.3's own "90k - 120k" case yields currency null.
        if record.salarySource == "ats":
            assert record.salaryCurrency, f"{provider}: structured salary, no currency, {record.id}"

    # Every row must survive `to_item`, which is what the dataset actually receives.
    assert all(item["recordType"] == "job" for item in (r.to_item("full") for r in records))


#: §5.12's degradation guard, restated as a blocking contract test. v1's mistake was
#: marking exactly this non-blocking, which is how the Workable class of failure ships.
DEGRADED_RATIO = 0.9
POPULATION: dict[str, tuple[str, ...]] = {
    "greenhouse": ("stripe", "cloudflare", "verkada", "rocketlab", "anthropic"),
    "lever": ("palantir", "zoox", "veeva", "spotify", "lever"),
    "ashby": ("openai", "snowflake", "notion", "cohere", "linear"),
    "recruitee": ("channable", "bunq", "greenchoice", "123fahrschule", "nmbrs"),
    "rippling": (
        "rippling",
        "riot-platforms-careers",
        "boom-supersonic",
        "kraken-robotics-inc",
        "atlas-data-storage",
    ),
    "personio": ("personio", "1komma5grad", "1sp-agency", "1000satellites-coworking", "sennder"),
}


async def test_ashby_compensation_interval_still_matches_what_we_parse() -> None:
    """§5.3: `ashby_interval()` reads `"1 YEAR"`. A v1 misreading of this exact field
    would have nulled every Ashby salary, so the shape is a blocking tripwire (V1 M7)."""
    ref = Ref(provider="ashby", slug=BOARDS["ashby"])
    async with make_client(timeout_secs=60.0) as client:
        records = await get_adapter("ashby").fetch(
            ref, client, options={**OPTIONS, "includeRawJson": True}
        )

    intervals = {
        component.get("interval")
        for record in records
        for component in (record.raw or {}).get("compensation", {}).get("summaryComponents") or []
    }
    seen = [value for value in intervals if value]
    assert seen, "no Ashby posting published an interval — the compensation object moved"
    assert all(re.fullmatch(r"\d+\s+[A-Z]+", value) for value in seen), seen


@pytest.mark.parametrize("provider", sorted(POPULATION))
async def test_no_provider_returns_a_population_of_empty_boards(provider: str) -> None:
    """§5.12: >90% of a provider's boards answering 200-with-zero-jobs is a broken
    provider, not five quiet companies. This must fail the contract suite (V1 M7)."""
    empty = 0
    async with make_client(timeout_secs=60.0) as client:
        for slug in POPULATION[provider]:
            ref = Ref(provider=provider, slug=slug)
            try:
                records = await get_adapter(provider).fetch(ref, client, options={})
            except Exception:  # noqa: BLE001 - a dark slug is not a degraded provider
                continue
            empty += not records
    ratio = empty / len(POPULATION[provider])
    assert ratio <= DEGRADED_RATIO, f"{provider}: {empty} of 5 boards returned zero jobs"
