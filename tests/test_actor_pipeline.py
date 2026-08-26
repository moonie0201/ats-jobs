"""End-to-end checks for the Actor pipeline (V1 M8: it had no tests at all).

`process_company`, `apply_delta`/`commit_delta`, `push_jobs`, `resolve_all` and
`flush_summaries` are where V1 B3, H1, H3, M1, M3, M6 and V3 S9 all lived. Everything
here drives the real functions against a fake adapter and the `test_billing.py` fake
Actor — no network, no Apify platform.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
ACTOR = ROOT / "actors" / "ats-jobs-scraper"
sys.path.append(str(ACTOR))

from src import main as shell  # noqa: E402

from core.billing import Billing  # noqa: E402
from core.filters import Filters  # noqa: E402
from core.models import STATUSES, JobRecord, Ref  # noqa: E402
from core.state import SeenState  # noqa: E402
from tests.test_billing import FakeActor  # noqa: E402


def ctx_for(actor: FakeActor, **overrides: Any) -> shell.RunCtx:
    cfg = shell.read_config(overrides)
    return shell.RunCtx(
        cfg=cfg,
        filters=Filters.from_input(cfg),
        billing=Billing(max_jobs=cfg["maxJobs"], actor=actor),
    )


def record(job_id: str, **kwargs: Any) -> JobRecord:
    return JobRecord(
        id=job_id,
        title=kwargs.pop("title", "Backend Engineer"),
        changeHash=kwargs.pop("changeHash", f"h-{job_id}"),
        **kwargs,
    )


class FakeAdapter:
    """Stands in for a `core.providers.*` module."""

    def __init__(self, records: list[JobRecord] | Exception):
        self._records = records
        self.options: dict[str, Any] | None = None

    async def fetch(self, ref: Ref, client: Any, options: dict[str, Any] | None = None) -> list:
        self.options = options
        if isinstance(self._records, Exception):
            raise self._records
        return list(self._records)


def install(monkeypatch: pytest.MonkeyPatch, adapter: FakeAdapter) -> FakeAdapter:
    monkeypatch.setattr(shell, "get_adapter", lambda provider: adapter)
    return adapter


async def state_with(ids: dict[str, dict[str, Any]] | None = None) -> SeenState:
    store: dict[str, Any] = {"seen": dict(ids or {})}

    class Store:
        async def get_value(self, key, default_value=None):
            return store.get(key, default_value)

        async def set_value(self, key, value, content_type=None):
            store[key] = value

    return await SeenState.open("k", opener=lambda _key: _ready(Store()))


async def _ready(value: Any) -> Any:
    return value


# --- V1 B3: delta marks what was delivered, never what was considered -----------------


async def test_a_row_the_cap_never_delivered_is_not_marked_as_seen(monkeypatch):
    """`maxJobsPerCompany` used to trim *after* `apply_delta` marked every kept row, so
    b and c were flagged seen, never pushed and never returned again (V1 B3)."""
    actor = FakeActor()
    ctx = ctx_for(actor, maxJobsPerCompany=1, onlyNewJobs=True)
    ctx.state = await state_with()
    install(monkeypatch, FakeAdapter([record("a"), record("b"), record("c")]))

    await shell.process_company(Ref(provider="greenhouse", slug="acme"), None, ctx)

    assert [item["id"] for item in actor.charged_rows] == ["a"]
    assert sorted(ctx.state.seen) == ["a"], "b and c were never delivered"
    summary = ctx.summaries[0][1]
    assert summary["newJobs"] == 1, "newJobs counts rows delivered, not rows marked"


async def test_a_row_maxjobs_never_delivered_is_not_marked_as_seen(monkeypatch):
    actor = FakeActor()
    ctx = ctx_for(actor, maxJobs=2, onlyNewJobs=True)
    ctx.state = await state_with()
    install(monkeypatch, FakeAdapter([record("a"), record("b"), record("c")]))

    await shell.process_company(Ref(provider="greenhouse", slug="acme"), None, ctx)

    assert [item["id"] for item in actor.charged_rows] == ["a", "b"]
    assert sorted(ctx.state.seen) == ["a", "b"]
    assert ctx.summaries[0][1]["status"] == "max_jobs_reached"


async def test_the_second_run_returns_what_the_first_run_could_not_deliver(monkeypatch):
    """The whole point of B3: nothing is lost, it is only postponed."""
    board = [record("a"), record("b"), record("c")]
    actor = FakeActor()
    ctx = ctx_for(actor, maxJobsPerCompany=1, onlyNewJobs=True)
    ctx.state = await state_with()
    install(monkeypatch, FakeAdapter(board))
    await shell.process_company(Ref(provider="greenhouse", slug="acme"), None, ctx)
    carried = dict(ctx.state.seen)

    actor2 = FakeActor()
    ctx2 = ctx_for(actor2, maxJobsPerCompany=1, onlyNewJobs=True)
    ctx2.state = await state_with(carried)
    install(monkeypatch, FakeAdapter([record("a"), record("b"), record("c")]))
    await shell.process_company(Ref(provider="greenhouse", slug="acme"), None, ctx2)

    assert [item["id"] for item in actor2.charged_rows] == ["b"]


async def test_apply_delta_decides_without_committing(monkeypatch):
    ctx = ctx_for(FakeActor(), onlyNewJobs=True)
    ctx.state = await state_with()
    rows = [record("a"), record("b")]
    emit, new_jobs = shell.apply_delta(ctx, rows)
    assert [r.id for r in emit] == ["a", "b"] and new_jobs == 0
    assert ctx.state.seen == {}, "deciding must not write state (V1 B3)"
    assert shell.commit_delta(ctx, emit[:1]) == 1
    assert list(ctx.state.seen) == ["a"]


async def test_without_a_state_store_isnew_stays_null(monkeypatch):
    actor = FakeActor()
    ctx = ctx_for(actor)
    install(monkeypatch, FakeAdapter([record("a")]))
    await shell.process_company(Ref(provider="greenhouse", slug="acme"), None, ctx)
    assert actor.charged_rows[0]["isNew"] is None
    assert ctx.summaries[0][1]["newJobs"] is None


# --- V1 H1: the per-company budget reaches the adapter -------------------------------


async def test_the_adapter_is_handed_a_deadline(monkeypatch):
    """Rippling issues one detail call per job against a 2 rps bucket; without a deadline
    it can see, a 374-job board overran the budget and delivered nothing (V1 H1)."""
    adapter = install(monkeypatch, FakeAdapter([record("a")]))
    ctx = ctx_for(FakeActor())
    await shell.process_company(Ref(provider="rippling", slug="acme"), None, ctx)
    assert adapter.options is not None
    assert adapter.options["deadline"] > 0
    assert adapter.options["maxJobs"] == ctx.cfg["maxJobs"], "the deadline rides along, not instead"


# --- V1 H3: an explicit prefix or URL always wins -------------------------------------


async def test_an_explicit_url_is_not_re_filtered_by_the_providers_list():
    actor = FakeActor()
    ctx = ctx_for(actor, providers=["greenhouse"])
    refs = await shell.resolve_all(["https://jobs.lever.co/palantir", "greenhouse:acme"], None, ctx)
    assert [(r.provider, r.slug) for r in refs] == [("lever", "palantir"), ("greenhouse", "acme")]
    assert actor.free_rows == [], "no error row: §4.1 says the URL wins"


# --- V1 M1 / M2: a degraded provider is not `ok` --------------------------------------


async def test_a_degraded_provider_is_not_reported_as_ok():
    actor = FakeActor()
    ctx = ctx_for(actor)
    ctx.companies_seen["lever"] = 6
    ctx.companies_zero["lever"] = 6
    for n in range(6):
        ctx.summaries.append(
            (
                "lever",
                shell.summary_item(Ref(provider="lever", slug=f"c{n}"), status="ok", jobs_found=0),
            )
        )
    await shell.flush_summaries(ctx)

    rows = actor.free_rows
    assert len(rows) == 6
    assert {row["status"] for row in rows} == {"provider_degraded"}
    assert all("provider_degraded" in row["warnings"] for row in rows)


def test_every_status_the_shell_pushes_is_in_the_vocabulary():
    """V1 M2: `provider_unavailable` was emitted but never declared."""
    for value in ("provider_unavailable", "provider_degraded", "max_jobs_reached"):
        assert value in STATUSES


async def test_summaries_are_flushed_once():
    """The ABORTING handler and the normal path must not double-push (V1 M4)."""
    actor = FakeActor()
    ctx = ctx_for(actor)
    summary = shell.summary_item(Ref(provider="lever", slug="c"), status="ok")
    ctx.summaries.append(("lever", summary))
    await shell.flush_summaries(ctx)
    await shell.flush_summaries(ctx)
    assert len(actor.free_rows) == 1


# --- V1 M3: companyDomain -------------------------------------------------------------


async def test_the_directory_domain_reaches_every_row(monkeypatch):
    actor = FakeActor()
    ctx = ctx_for(actor)
    install(monkeypatch, FakeAdapter([record("a")]))
    ref = Ref(provider="greenhouse", slug="anthropic", domain="anthropic.com")
    await shell.process_company(ref, None, ctx)
    assert actor.charged_rows[0]["companyDomain"] == "anthropic.com"
    assert ctx.summaries[0][1]["companyDomain"] == "anthropic.com"


# --- V1 M6: content dedupe across companies -------------------------------------------


async def test_content_dedupe_records_the_merge_across_companies(monkeypatch):
    actor = FakeActor()
    ctx = ctx_for(actor, dedupe="content")
    install(monkeypatch, FakeAdapter([record("a", contentKey="k")]))
    await shell.process_company(Ref(provider="greenhouse", slug="one"), None, ctx)
    install(monkeypatch, FakeAdapter([record("b", contentKey="k")]))
    await shell.process_company(Ref(provider="lever", slug="two"), None, ctx)

    assert [item["id"] for item in actor.charged_rows] == ["a"]
    survivor = ctx.content_survivors["k"]
    assert survivor.dedupedFrom == ["b"], "§4.5.6 correction 2 is unconditional"
    assert ctx.summaries[1][1]["duplicatesDropped"] == 1


# --- V3 S9 / S13: a failure is this company's, not the run's ---------------------------


async def test_a_push_failure_becomes_an_error_row(monkeypatch):
    class Exploding(FakeActor):
        async def push_data(self, data, *, charged_event_name=None):
            if charged_event_name == "job":
                raise RuntimeError("/srv/secret/path exploded")
            return await super().push_data(data, charged_event_name=charged_event_name)

    actor = Exploding()
    ctx = ctx_for(actor)
    install(monkeypatch, FakeAdapter([record("a")]))
    await shell.process_company(Ref(provider="greenhouse", slug="acme"), None, ctx)

    assert [row["recordType"] for row in actor.free_rows] == ["error"]
    assert "/srv/secret/path" not in actor.free_rows[0]["error"], "V3 S13: no internals"


async def test_an_adapter_failure_never_leaks_internals(monkeypatch):
    actor = FakeActor()
    ctx = ctx_for(actor)
    install(monkeypatch, FakeAdapter(RuntimeError("/srv/secret/path exploded")))
    await shell.process_company(Ref(provider="greenhouse", slug="acme"), None, ctx)
    assert "/srv/secret/path" not in actor.free_rows[0]["error"]
    assert actor.free_rows[0]["status"] in STATUSES


async def test_one_worker_crash_does_not_take_the_run_down(monkeypatch):
    """V3 S9: `return_exceptions=False` orphaned the siblings and skipped the state save."""
    calls: list[str] = []

    async def flaky(ref, client, ctx):
        calls.append(ref.slug)
        if ref.slug == "boom":
            raise RuntimeError("kaboom")

    monkeypatch.setattr(shell, "process_company", flaky)
    ctx = ctx_for(FakeActor())
    queue: Any = shell.asyncio.Queue()
    for slug in ("boom", "ok1", "ok2"):
        queue.put_nowait(Ref(provider="greenhouse", slug=slug))
    outcomes = await shell.asyncio.gather(
        shell.worker(queue, None, ctx), shell.worker(queue, None, ctx), return_exceptions=True
    )
    assert any(isinstance(o, BaseException) for o in outcomes)
    assert set(calls) == {"boom", "ok1", "ok2"}


# --- V1 M5: the state store degrades instead of failing the run -----------------------


async def test_a_rejected_state_store_disables_onlynewjobs_instead_of_failing(monkeypatch):
    actor = FakeActor()
    ctx = ctx_for(actor, onlyNewJobs=True, stateKey="ats.jobs_state")

    async def boom(*_args, **_kwargs):
        raise RuntimeError("invalid store name")

    monkeypatch.setattr(shell.SeenState, "open", boom)
    await shell.open_state(ctx)

    assert ctx.state is None and ctx.store is None
    assert actor.free_rows[0]["recordType"] == "error"
    assert actor.free_rows[0]["status"] in STATUSES


async def test_a_non_dict_company_state_does_not_crash(monkeypatch):
    ctx = ctx_for(FakeActor(), onlyNewJobs=True)

    class Store:
        async def get_value(self, key, default_value=None):
            return "not a dict"

        async def set_value(self, key, value, content_type=None):
            return None

    monkeypatch.setattr(shell.SeenState, "open", lambda *a, **k: _ready(SeenState()))
    monkeypatch.setattr(shell.Actor, "open_key_value_store", lambda **k: _ready(Store()))
    await shell.open_state(ctx)
    assert ctx.company_state == {}  # V3 S14


# --- V1 L4 --------------------------------------------------------------------------


def test_companies_are_truncated_to_the_schema_limit():
    entries = [f"greenhouse:c{n}" for n in range(shell.MAX_COMPANIES + 500)]
    assert len(shell.read_companies({"companies": entries})) == shell.MAX_COMPANIES


# --- V1 H2 / V3 S10: the directory is reachable ---------------------------------------


async def test_the_directory_is_loaded_with_the_runs_http_client(monkeypatch):
    """`get_directory()` with no client left `load_directory` with an empty source list,
    so every bare slug became an error row while the schema advertised the opposite."""
    seen: list[Any] = []

    async def fake_get_directory(client=None, **kwargs):
        seen.append(client)
        return None

    monkeypatch.setattr(shell, "get_directory", fake_get_directory)
    sentinel = object()
    await shell.resolve_all(["anthropic"], sentinel, ctx_for(FakeActor()))
    assert seen == [sentinel], "the client is what unlocks the jsDelivr/raw sources"


async def test_the_directory_is_not_loaded_when_nothing_needs_it(monkeypatch):
    calls: list[Any] = []
    monkeypatch.setattr(shell, "get_directory", lambda *a, **k: calls.append(a) or _ready(None))
    await shell.resolve_all(["greenhouse:acme"], object(), ctx_for(FakeActor()))
    assert calls == [], "§6.6: a run made of prefixes never downloads the directory"
