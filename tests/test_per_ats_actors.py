"""The three per-ATS listings A2–A4 (SPEC v2 §3.2, §8.2, §13.5).

Each one is a ~30-line `src/main.py` that pins :func:`core.run.main` to one provider, so
what needs locking down is not the pipeline — `test_actor_pipeline.py` owns that — but the
three things a fork could silently get wrong:

* the pin actually reaches the run, so a Lever URL pasted into the Greenhouse listing
  becomes a free error row instead of a charged Lever job (a Congruency failure the buyer
  would pay for);
* a **bare** token resolves, because that is the only input shape these listings' buyers
  will type, and the multi-ATS Actor refuses it by design (§5.11 rule 5);
* the run still charges `job` and nothing else, against the provider's own committed
  fixture, with rows valid under that Actor's own dataset schema.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core import run as shell
from core.billing import Billing
from core.filters import Filters
from core.http import make_client
from core.resolve import Unresolved, resolve
from tests.test_billing import FakeActor

ROOT = Path(__file__).resolve().parent.parent

#: provider -> (Actor directory, board slug, fixture, the URL respx must serve, another
#: provider's entry that this listing has to refuse).
ACTORS: dict[str, tuple[str, str, str, str, str]] = {
    "greenhouse": (
        "greenhouse-jobs-scraper",
        "anthropic",
        "anthropic.json",
        "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs",
        "https://jobs.lever.co/palantir",
    ),
    "lever": (
        "lever-jobs-scraper",
        "palantir",
        "palantir.json",
        "https://api.lever.co/v0/postings/palantir",
        "https://jobs.ashbyhq.com/openai",
    ),
    "ashby": (
        "ashby-jobs-scraper",
        "openai",
        "openai.json",
        "https://api.ashbyhq.com/posting-api/job-board/openai",
        "greenhouse:anthropic",
    ),
}


def actor_dir(provider: str) -> Path:
    return ROOT / "actors" / ACTORS[provider][0]


def dataset_fields(provider: str) -> set[str]:
    schema = actor_dir(provider) / ".actor" / "dataset_schema.json"
    return set(json.loads(schema.read_text(encoding="utf-8"))["fields"]["properties"])


@pytest.fixture
def client():
    """A client whose backoffs and rate-cap waits are instant."""
    clock = [0.0]

    async def fake_sleep(delay: float) -> None:
        clock[0] += delay

    return make_client(timeout_secs=5, sleep=fake_sleep, clock=lambda: clock[0])


def ctx_for(provider: str, actor: FakeActor, **overrides: Any) -> shell.RunCtx:
    cfg = shell.read_config(overrides, provider)
    return shell.RunCtx(
        cfg=cfg,
        filters=Filters.from_input(cfg),
        billing=Billing(max_jobs=cfg["maxJobs"], actor=actor),
        provider=provider,
    )


# ----------------------------------------------------------- the run, per Actor


@pytest.mark.parametrize("provider", sorted(ACTORS))
async def test_actor_delivers_valid_rows_and_charges_only_job(provider, fixture, client):
    """One run of each per-ATS Actor against that provider's committed fixture."""
    _dir, slug, payload, url, _foreign = ACTORS[provider]
    actor = FakeActor()
    ctx = ctx_for(provider, actor)

    async with client as http:
        with respx.mock:
            respx.get(url).mock(return_value=httpx.Response(200, json=fixture(provider, payload)))
            refs = await shell.resolve_all([slug], http, ctx)
            assert [(r.provider, r.slug) for r in refs] == [(provider, slug)]
            await shell.process_company(refs[0], http, ctx)
        await shell.flush_summaries(ctx)

    jobs = actor.charged_rows
    assert jobs, f"{provider}: the fixture board delivered no job rows"
    known = dataset_fields(provider)
    for item in jobs:
        assert item["recordType"] == "job"
        assert item["provider"] == provider
        assert item["companySlug"] == slug
        assert item["id"].startswith(f"{provider}:{slug}:")
        assert item["title"]
        assert set(item) <= known, f"{provider}: {set(item) - known} is not in dataset_schema"

    # §8.2 / G5: `job` is the only paid event, and it is charged once per delivered row.
    assert {event for _item, event in actor.pushed} == {"job", None}
    assert len(jobs) == ctx.billing.jobs_pushed
    assert actor.charges == [], "delta-run must not fire with onlyNewJobs off"
    summaries = [item for item in actor.free_rows if item["recordType"] == "company_summary"]
    assert [s["status"] for s in summaries] == ["ok"]
    assert summaries[0]["jobsFound"] == len(jobs)


# -------------------------------------------------------------------- the pin


@pytest.mark.parametrize("provider", sorted(ACTORS))
def test_a_bare_token_is_that_providers_board_slug(provider):
    """The multi-ATS Actor needs the directory for this; a pinned one has nothing to guess."""
    slug = ACTORS[provider][1]
    ref = resolve(slug, pin=provider)
    assert not isinstance(ref, Unresolved)
    assert (ref.provider, ref.slug) == (provider, slug)
    assert isinstance(resolve(slug), Unresolved), "unpinned, a bare token still needs the directory"


@pytest.mark.parametrize("provider", sorted(ACTORS))
async def test_another_ats_is_refused_rather_than_fetched(provider, client):
    """A foreign board must become a free error row naming the right Actor — never a charge."""
    foreign = ACTORS[provider][4]
    actor = FakeActor()
    ctx = ctx_for(provider, actor)

    async with client as http:
        refs = await shell.resolve_all([foreign], http, ctx)

    assert refs == []
    assert actor.charged_rows == []
    (error,) = actor.free_rows
    assert error["recordType"] == "error"
    assert error["status"] == "not_found"
    assert provider in error["error"] and "ats-jobs-scraper" in error["error"]


# ------------------------------------------------------- the listing's own files


@pytest.mark.parametrize("provider", sorted(ACTORS))
def test_the_shipped_entrypoint_pins_the_provider_it_is_named_for(provider):
    """The pin lives in one line of `src/main.py`; a copy-paste slip there is invisible
    until a buyer on the Ashby listing is charged for Greenhouse jobs."""
    name = ACTORS[provider][0]
    source = (actor_dir(provider) / "src" / "main.py").read_text(encoding="utf-8")
    assert f'PROVIDER = "{provider}"' in source
    assert "from core.run import main" in source, "a per-ATS listing is glue, not a fork"

    schema = json.loads(
        (actor_dir(provider) / ".actor" / "input_schema.json").read_text(encoding="utf-8")
    )
    assert "providers" not in schema["properties"], "§3.2: the selector is removed, not defaulted"
    assert schema["properties"]["stateKey"]["default"] == f"{provider}-jobs-state-default"
    assert (
        json.loads((actor_dir(provider) / ".actor" / "actor.json").read_text(encoding="utf-8"))[
            "name"
        ]
        == name
    )


@pytest.mark.parametrize("provider", sorted(ACTORS))
def test_the_default_state_key_is_namespaced_per_actor(provider):
    """The named KV store is account-wide: a shared default would have one listing
    marking ids that then went missing from another listing's delta."""
    cfg = shell.read_config({}, provider)
    assert cfg["stateKey"] == f"{provider}-jobs-state-default"
    assert cfg["providers"] == [provider]
    assert shell.read_config({"providers": ["lever", "ashby"]}, provider)["providers"] == [provider]
