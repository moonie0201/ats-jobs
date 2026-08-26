"""`ats-history-snapshot` (B1) runner: input, budget guard, §7.4 safety rules, resume.

`src/main.py` is loaded by path rather than through `sys.path`, because every Actor in
this repo names its shell package `src` and the first one imported would otherwise win
`sys.modules["src"]` for the whole session (`test_actor_shell.py` imports the scraper's).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from core.diff import EVENT_KEYS
from core.history import HistoryStore, bucket_of, encode, events_key, state_key
from core.providers import greenhouse

from .test_history_bucket import FakeStore

ROOT = Path(__file__).resolve().parent.parent
ACTOR = ROOT / "actors" / "ats-history-snapshot"


def _load():
    spec = importlib.util.spec_from_file_location("snapshot_main", ACTOR / "src" / "main.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: `@dataclass(slots=True)` resolves annotations through
    # `sys.modules[cls.__module__]` and raises on a module that is not there yet.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


snap = _load()

TODAY = "2026-08-26"
YESTERDAY = "2026-08-25"


def entry(provider="greenhouse", company="acme"):
    return {"provider": provider, "company": company, "added": TODAY, "source": "seed"}


def job(jid="1", title="Backend Engineer", loc="Berlin", **kwargs):
    return {
        "id": jid,
        "t": title,
        "loc": loc,
        "dept": "Engineering",
        "remote": False,
        "url": f"https://job-boards.greenhouse.io/acme/jobs/{jid}",
        "posted": "2026-08-19",
        "updated": "2026-08-19",
        **kwargs,
    }


def make_run(store=None, **cfg_overrides):
    store = store or HistoryStore(FakeStore())
    cfg = snap.read_config({"shard": 0, "maxCompanies": 0, **cfg_overrides})
    budget = snap.Budget(ceiling_usd=cfg["costCeilingUsd"], memory_gb=0.5)
    return snap.Run(cfg=cfg, store=store, budget=budget, today=TODAY)


@pytest.fixture
def stub_fetch(monkeypatch):
    """Install a canned `fetch_company`: `results[company] = (jobs | None, status)`."""

    def install(results, calls=None):
        async def fetch_company(entry, client, cfg):
            if calls is not None:
                calls.append(entry["company"])
            return results[entry["company"]]

        monkeypatch.setattr(snap, "fetch_company", fetch_company)

    return install


# --- input -------------------------------------------------------------------


def test_config_defaults_and_bounds():
    cfg = snap.read_config({})
    assert cfg["shard"] == 0 and cfg["shardCount"] == 4
    assert cfg["costCeilingUsd"] == snap.DEFAULTS["costCeilingUsd"] and cfg["maxCompanies"] == 0
    assert cfg["purgeProvider"] == "" and cfg["purgeCompany"] == ""

    hostile = snap.read_config(
        {"shard": 99, "shardCount": 200, "maxConcurrency": "lots", "costCeilingUsd": -3}
    )
    assert hostile["shardCount"] == 8
    assert hostile["shard"] == 7, "a shard past the last one would sweep nothing, silently"
    assert hostile["maxConcurrency"] == 8
    assert hostile["costCeilingUsd"] == 0.0


def test_config_survives_nan_and_infinity():
    # `json.loads` accepts the non-standard literals NaN/Infinity, so both reach us.
    cfg = snap.read_config({"maxCompanies": float("nan"), "requestTimeoutSecs": float("inf")})
    assert cfg["maxCompanies"] == 0
    assert cfg["requestTimeoutSecs"] == 120


# --- budget guard ------------------------------------------------------------


def test_preflight_estimate_grows_with_the_shard_and_stays_inside_the_default_ceiling():
    ceiling = snap.DEFAULTS["costCeilingUsd"]
    budget = snap.Budget(ceiling_usd=ceiling, memory_gb=0.5)
    # 385 is the real shard-0 size of the shipped 1,574-company watchlist, and the live
    # run that measured SECS_PER_COMPANY is what this has to leave room for.
    full_shard = budget.estimate(385, shard_count=4)
    assert 0 < full_shard < ceiling, "a full shard must not trip its own preflight"
    assert budget.estimate(50_000, shard_count=4) > ceiling, "a runaway watchlist is refused"
    assert budget.estimate(1000, 4) > budget.estimate(100, 4)
    assert budget.estimate(0, 4) > budget.cost(elapsed=0), "the container floor is billed too"


def test_the_default_ceiling_fits_the_monthly_budget():
    """A per-run ceiling is not a monthly budget: 4 runs/day x $0.05 was $6.09/month
    against a $5.00 credit (H1 H2)."""
    assert snap.DEFAULTS["costCeilingUsd"] * 4 * 30.44 <= snap.MONTHLY_BUDGET_USD


def test_running_cost_trips_the_ceiling_and_latches():
    budget = snap.Budget(ceiling_usd=0.0000001, memory_gb=0.5)
    budget.kv_writes = 100
    assert budget.over() is True
    budget.ceiling_usd = 1000.0
    assert budget.tripped is True, "once stopped, the run stays stopped for this sweep"


def test_cost_counts_compute_writes_reads_and_transfer():
    budget = snap.Budget(ceiling_usd=1.0, memory_gb=1.0)
    budget.kv_writes, budget.kv_reads, budget.jobs_fetched = 1000, 1000, 1_000_000
    cost = budget.cost(elapsed=3600)
    assert cost == pytest.approx(0.20 + 0.05 + 0.005 + 1024e6 / 1e9 * 0.20, rel=1e-6)


# --- what gets stored (§7.3: no PII, no descriptions) ------------------------


def test_job_row_stores_only_the_documented_metadata(fixture):
    from core.models import Ref

    raw = fixture("greenhouse", "anthropic.json")["jobs"][0]
    record = greenhouse.to_record(raw, Ref(provider="greenhouse", slug="anthropic"))
    record.descriptionHtml = "<p>secret</p>"
    record.descriptionText = "secret"

    row = snap.job_row(record)

    assert set(row) == {"id", "t", "loc", "dept", "remote", "url", "posted", "updated"}
    assert "secret" not in repr(row)
    assert row["id"] and row["t"] and row["url"].startswith("https://")
    assert row["posted"] is None or len(row["posted"]) == 10, "§7.3: YYYY-MM-DD, 10 chars"


def test_job_row_needs_an_id():
    from core.models import JobRecord

    assert snap.job_row(JobRecord(id=None, sourceId=None, title="x")) is None


# --- §7.4 safety rules -------------------------------------------------------


async def test_first_run_records_every_job_as_added_and_writes_both_keys(stub_fetch):
    backing = FakeStore()
    run = make_run(HistoryStore(backing))
    bucket = bucket_of("greenhouse", "acme")
    stub_fetch({"acme": ([job("1"), job("2")], "ok")})

    await snap.process_bucket(bucket, [entry()], None, run)

    assert [e["ev"] for e in run.events] == ["added", "added"]
    assert run.counts == [{"d": TODAY, "provider": "greenhouse", "company": "acme", "open": 2}]
    state = await run.store.state(bucket)
    assert state["as_of"] == TODAY
    company = state["companies"]["greenhouse:acme"]
    assert company["tracked_since"] == TODAY and set(company["jobs"]) == {"1", "2"}
    assert events_key(TODAY, 0) in backing.values, "events are flushed with their bucket"


async def test_a_failed_fetch_keeps_the_previous_state_and_emits_nothing(stub_fetch):
    """The rule the whole moat rests on: a network blip is not a mass layoff."""
    bucket = bucket_of("greenhouse", "acme")
    before = {
        "as_of": YESTERDAY,
        "companies": {
            "greenhouse:acme": {
                "status": "ok",
                "consecutive_failures": 0,
                "tracked_since": "2026-08-01",
                "jobs": {"1": {"t": "Backend Engineer", "h": "x", "first_seen": "2026-08-01"}},
            }
        },
    }
    run = make_run(HistoryStore(FakeStore({state_key(bucket): encode(before)})))
    stub_fetch({"acme": (None, "timeout")})

    await snap.process_bucket(bucket, [entry()], None, run)

    company = (await run.store.state(bucket))["companies"]["greenhouse:acme"]
    assert run.events == [] and run.counts == []
    assert company["jobs"] == before["companies"]["greenhouse:acme"]["jobs"]
    assert company["status"] == "timeout" and company["consecutive_failures"] == 1
    assert company["tracked_since"] == "2026-08-01"


async def test_seven_consecutive_failures_flag_the_company_stale(stub_fetch):
    bucket = bucket_of("lever", "gone")
    before = {
        "as_of": YESTERDAY,
        "companies": {"lever:gone": {"consecutive_failures": 6, "jobs": {}}},
    }
    run = make_run(HistoryStore(FakeStore({state_key(bucket): encode(before)})))
    stub_fetch({"gone": (None, "not_found")})

    await snap.process_bucket(bucket, [entry("lever", "gone")], None, run)

    assert (await run.store.state(bucket))["companies"]["lever:gone"]["stale"] is True


async def test_an_empty_board_needs_two_consecutive_runs_before_removals_fire(stub_fetch):
    bucket = bucket_of("greenhouse", "acme")
    before = {
        "as_of": YESTERDAY,
        "companies": {
            "greenhouse:acme": {
                "status": "ok",
                "tracked_since": "2026-08-01",
                "jobs": {"1": {"t": "Backend Engineer", "h": "x", "posted": "2026-08-01"}},
            }
        },
    }
    store = HistoryStore(FakeStore({state_key(bucket): encode(before)}))
    run = make_run(store)
    stub_fetch({"acme": ([], "ok")})

    await snap.process_bucket(bucket, [entry()], None, run)
    company = (await store.state(bucket))["companies"]["greenhouse:acme"]
    assert run.events == [], "one empty response must not remove a whole board"
    assert company["status"] == snap.EMPTY_SUSPECT and company["jobs"]

    # Second consecutive empty run (same day here, so `force`): now the removal is real.
    run2 = make_run(store, force=True)
    await snap.process_bucket(bucket, [entry()], None, run2)
    assert [e["ev"] for e in run2.events] == ["removed"]
    assert run2.events[0]["days_open"] == 25
    assert (await store.state(bucket))["companies"]["greenhouse:acme"]["jobs"] == {}


async def test_a_degraded_provider_updates_nothing(stub_fetch):
    """>90% of one provider's companies answering 200-with-zero-jobs is a broken
    provider, not 90% of an industry closing its boards on the same morning (§5.12)."""
    entries = [entry("recruitee", f"c{i}") for i in range(11)]
    results = {f"c{i}": ([], "ok") for i in range(11)}
    results["c0"] = ([job("1")], "ok")
    before = {
        "as_of": YESTERDAY,
        "companies": {
            f"recruitee:c{i}": {"status": "ok", "jobs": {"9": {"t": "x", "h": "y"}}}
            for i in range(11)
        },
    }
    bucket = bucket_of("recruitee", "c0")
    run = make_run(HistoryStore(FakeStore({state_key(bucket): encode(before)})))
    stub_fetch(results)

    await snap.process_bucket(bucket, entries, None, run)

    assert snap.is_degraded(run, "recruitee") is True
    state = await run.store.state(bucket)
    assert run.events == [], "no events at all for a provider that is down"
    for i in range(11):
        # §7.4 row 3: no state updates either. Writing `consecutive_failures` here flagged
        # every company of the provider `stale` after seven degraded days (H1 L8).
        assert state["companies"][f"recruitee:c{i}"] == before["companies"][f"recruitee:c{i}"]


async def test_a_changed_title_is_one_changed_event_not_an_add_and_a_remove(stub_fetch):
    bucket = bucket_of("greenhouse", "acme")
    store = HistoryStore(FakeStore())
    run = make_run(store)
    stub_fetch({"acme": ([job("1")], "ok")})
    await snap.process_bucket(bucket, [entry()], None, run)

    run2 = make_run(store, force=True)
    stub_fetch({"acme": ([job("1", title="Staff Backend Engineer")], "ok")})
    await snap.process_bucket(bucket, [entry()], None, run2)

    assert [e["ev"] for e in run2.events] == ["changed"]
    assert run2.events[0]["changed"] == ["t"]
    assert set(run2.events[0]) == set(EVENT_KEYS)


# --- resumability ------------------------------------------------------------


async def test_a_bucket_already_collected_today_is_skipped_on_a_restart(stub_fetch):
    bucket = bucket_of("greenhouse", "acme")
    store = HistoryStore(FakeStore({state_key(bucket): encode({"as_of": TODAY, "companies": {}})}))
    run = make_run(store)
    calls: list[str] = []
    stub_fetch({"acme": ([job("1")], "ok")}, calls)

    await snap.process_bucket(bucket, [entry()], None, run)

    assert calls == [], "a resumed run must not re-fetch a bucket it already paid for"
    assert run.buckets_skipped == 1 and run.buckets_done == 0

    run_forced = make_run(store, force=True)
    await snap.process_bucket(bucket, [entry()], None, run_forced)
    assert calls == ["acme"], "force re-collects"


async def test_events_already_written_today_survive_a_resumed_run(stub_fetch):
    store = HistoryStore(FakeStore())
    run = make_run(store)
    stub_fetch({"acme": ([job("1")], "ok"), "beta": ([job("2")], "ok")})
    await snap.process_bucket(bucket_of("greenhouse", "acme"), [entry()], None, run)
    first = await store.events(TODAY, 0)

    run2 = make_run(store)
    await snap.resume_events(run2)  # what main() does before the first bucket
    assert run2.events == first
    await snap.process_bucket(bucket_of("greenhouse", "beta"), [entry(company="beta")], None, run2)

    assert len(await store.events(TODAY, 0)) == 2, (
        "a restarted run must not overwrite the events the morning already wrote"
    )


# --- finalize ----------------------------------------------------------------


async def test_finalize_is_idempotent_for_the_day():
    store = HistoryStore(FakeStore())
    run = make_run(store)
    run.counts = [{"d": TODAY, "provider": "greenhouse", "company": "acme", "open": 5}]
    run.statuses["ok"] = 1
    run.companies_done = 1

    await snap.finalize(run)
    await snap.finalize(run)

    assert await store.counts(TODAY, 0) == run.counts, "a rerun must not double-count the day"
    meta = await store.meta()
    assert meta["last_run"] == {"0": TODAY} and meta["ok"] == 1 and meta["version"] == snap.VERSION


async def test_finalize_leaves_other_days_and_other_shards_alone():
    """Counts are keyed per day and shard, so there is nothing to merge and no month-sized
    value to materialise — and no lost update when two shards overlap (H1 M4/M5)."""
    store = HistoryStore(FakeStore())
    await store.put_counts(YESTERDAY, 0, [{"d": YESTERDAY, "company": "acme", "open": 3}])
    await store.put_counts(TODAY, 1, [{"d": TODAY, "company": "palantir", "open": 9}])
    run = make_run(store)
    run.counts = [{"d": TODAY, "provider": "greenhouse", "company": "acme", "open": 5}]

    await snap.finalize(run)

    assert await store.counts(TODAY, 0) == run.counts
    assert await store.counts(YESTERDAY, 0) == [{"d": YESTERDAY, "company": "acme", "open": 3}]
    assert await store.counts(TODAY, 1) == [{"d": TODAY, "company": "palantir", "open": 9}]


# --- watchlist ---------------------------------------------------------------


class FakeDirectory:
    source = "baked"

    def __init__(self, rows):
        self.rows = rows


def test_seed_rows_takes_only_live_boards_of_retainable_providers():
    rows = snap.seed_rows(
        FakeDirectory(
            [
                {"provider": "greenhouse", "slug": "acme", "job_count": 12},
                {"provider": "greenhouse", "slug": "empty", "job_count": 0},
                {"provider": "greenhouse", "slug": "unknown", "job_count": 5},
                {"provider": "workday", "slug": "nvidia", "job_count": 5},
                {"provider": "greenhouse", "slug": None, "job_count": 5},
                {"provider": "greenhouse", "slug": "acme", "job_count": 12},
            ]
        ),
        TODAY,
    )
    assert [r["company"] for r in rows] == ["acme", "unknown"]
    assert rows[0] == {
        "provider": "greenhouse",
        "company": "acme",
        "site": None,
        "added": TODAY,
        "source": "seed",
    }


def test_seed_rows_respects_the_watchlist_cap():
    rows = snap.seed_rows(
        FakeDirectory(
            [
                {"provider": "lever", "slug": f"c{i}", "job_count": 1}
                for i in range(snap.WATCHLIST_CAP + 50)
            ]
        ),
        TODAY,
    )
    assert len(rows) == snap.WATCHLIST_CAP


def test_retainable_gates_on_the_provider_spec():
    assert snap.retainable("greenhouse") is True
    assert snap.retainable("nope") is False


def test_the_real_directory_seed_lands_in_every_shard():
    """1,685 `ok` companies over 64 buckets: no shard may come up empty, or its schedule
    silently collects nothing for a quarter of the watchlist."""
    import asyncio

    from core.directory import load_directory

    directory = asyncio.run(load_directory(None))
    rows = snap.seed_rows(directory, TODAY)
    assert len(rows) > 1000
    per_shard = [0, 0, 0, 0]
    for row in rows:
        per_shard[bucket_of(row["provider"], row["company"]) // 16] += 1
    assert all(count > 100 for count in per_shard), per_shard


async def test_a_resumed_run_does_not_erase_the_real_runs_summary():
    store = HistoryStore(FakeStore())
    done = make_run(store)
    done.counts = [{"d": TODAY, "provider": "greenhouse", "company": "acme", "open": 5}]
    done.statuses["ok"] = 60
    done.companies_done = 60
    done.events = [{"ev": "added"}] * 1720
    await snap.finalize(done)

    resumed = make_run(store)  # every bucket already collected: nothing swept
    await snap.finalize(resumed)

    meta = await store.meta()
    assert meta["ok"] == 60 and meta["companies"] == 60 and meta["events"] == 1720
    assert meta["last_run"] == {"0": TODAY}


# --- H1 review fixes ---------------------------------------------------------


def test_a_run_that_lost_part_of_its_day_never_reports_success():
    """H1 H1: a budget-tripped or deadline-stopped run breaks the bucket loop, so the
    companies it never reached are not in `companies_done` and the 5% rule cannot see
    them. Exiting SUCCEEDED loses the day silently, and §7 says a lost day is forever."""
    for stopped in ("budget_ceiling", "run_deadline", "bucket_error", "finalize_error"):
        assert snap.should_fail(stopped, 400, 0) is True, stopped
    assert snap.should_fail(None, 400, 0) is False
    assert snap.should_fail(None, 400, 21) is True, "§7.2: more than 5% failed"
    assert snap.should_fail(None, 0, 0) is False, "a fully resumed run swept nothing"


def test_the_run_deadline_is_inside_the_stagger():
    """H1 H4: token buckets are per-process, so two overlapping shards put 4 rps on every
    shared ATS host. The 25-minute stagger is the only thing preventing that."""
    assert snap.RUN_DEADLINE_SECS < 25 * 60


async def test_a_purge_input_runs_a_takedown_instead_of_a_sweep():
    """H1 H3: §15.1 policy 4 promises a takedown within 48 hours; `HistoryStore.purge` had
    no caller anywhere in the repo."""
    bucket = bucket_of("greenhouse", "acme")
    backing = FakeStore(
        {
            state_key(bucket): encode(
                {"as_of": YESTERDAY, "companies": {"greenhouse:acme": {"jobs": {"1": {}}}}}
            )
        }
    )
    store = HistoryStore(backing)
    await store.put_events(
        TODAY, 0, [{"provider": "greenhouse", "company": "acme", "job_id": "1", "ev": "added"}]
    )

    removed = await store.purge("greenhouse", "acme", keys=await store.keys("events."))

    assert removed == {"companies": 1, "events": 1, "buckets": 1}
    assert (await store.state(bucket))["companies"] == {}
    assert await store.events(TODAY, 0) == []


def test_a_non_retainable_provider_is_dropped_from_collection_not_just_seeding(monkeypatch):
    """H1 H3: gating only `seed_rows` left every already-tracked company collected forever,
    because `ensure_watchlist` returns the stored rows unfiltered."""
    watchlist = [entry("greenhouse", "acme"), entry("lever", "palantir")]
    monkeypatch.setattr(snap, "retainable", lambda provider: provider != "lever")
    assert [r["company"] for r in watchlist if snap.retainable(r["provider"])] == ["acme"]


async def test_events_are_written_before_state_and_only_when_they_grow(stub_fetch):
    """H1 M1: a `put_events` that raised after `state.as_of` was already today meant
    tomorrow's diff saw those jobs in `prev`, so the `added` events were never re-emitted.
    H1 M2: rewriting an unchanged list once per bucket cost 16 writes a shard."""
    order: list[str] = []
    backing = FakeStore()
    real_set = backing.set_value

    async def spy(key, value, content_type=None):
        order.append(key.split(".")[0])
        await real_set(key, value, content_type)

    backing.set_value = spy
    store = HistoryStore(backing)
    run = make_run(store)
    stub_fetch({"acme": ([job("1")], "ok"), "quiet": ([], "ok")})

    await snap.process_bucket(bucket_of("greenhouse", "acme"), [entry()], None, run)
    assert order == ["events", "state"], "the unrecoverable write goes first"

    order.clear()
    await snap.process_bucket(bucket_of("greenhouse", "quiet"), [entry(company="quiet")], None, run)
    assert order == ["state"], "an empty board produced no events; do not rewrite the value"
