"""`ats-history-snapshot` — B1, the history moat (SPEC v2 §7). **Private, scheduled.**

One shard of the watchlist per run, four staggered runs a day (§7.2). Per run:

    watchlist (§7.6) -> buckets owned by this shard (§7.3) -> adapter fetch, metadata only
    -> core.diff (§7.4) -> state/{bucket} + events/{date}/{shard} -> counts + meta

This Actor sells nothing and pushes no dataset rows. Everything it costs is platform
cost, so the only budget that matters is the *platform* one. A full shard measured
$0.0157, and :class:`Budget` aborts before a bug can spend more than `costCeilingUsd`
(default $0.025) of it. A per-run ceiling is not a monthly budget, though --- 4 runs/day
at the old $0.05 was $6.09/month against a $5.00 credit --- so the run also refuses to
start once `meta.spend` says this month is already past :data:`MONTHLY_BUDGET_USD`.

Two rules the runner exists to enforce, both from §7.4's safety table — a network blip
must never look like a mass layoff, and a lost day is lost forever:

* a failed fetch keeps the previous state untouched and emits **no** events;
* every bucket is checkpointed the moment it is diffed, so a 90-minute timeout costs the
  buckets not yet reached and nothing else.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from apify import Actor, Event

from core.diff import diff
from core.directory import load_directory
from core.history import (
    BUCKETS,
    STORE_NAME,
    HistoryStore,
    bucket_of,
    buckets_for_shard,
)
from core.http import FetchError, make_client
from core.models import PROVIDERS, Ref
from core.providers import AdapterNotFound, get_adapter

VERSION = 1

DEFAULTS: dict[str, Any] = {
    "shard": 0,
    "shardCount": 4,
    "maxCompanies": 0,  # 0 = the whole shard
    "costCeilingUsd": 0.025,
    "maxConcurrency": 8,
    "requestTimeoutSecs": 30,
    "reseedWatchlist": False,
    "force": False,
    "purgeProvider": "",
    "purgeCompany": "",
}

NUMERIC_BOUNDS: dict[str, tuple[float, float]] = {
    "shard": (0, 7),
    "shardCount": (1, 8),
    "maxCompanies": (0, 20_000),
    "costCeilingUsd": (0.0, 5.0),
    "maxConcurrency": (1, 16),
    "requestTimeoutSecs": (5, 120),
}

#: §7.6 — seeded with every directory company validated `ok` with `job_count > 0`.
WATCHLIST_CAP = 5000

#: §5.12 / §7.4: ">90% of one provider's companies return 200-with-zero-jobs". The minimum
#: sample stops a three-company shard from libelling a provider over one empty board.
DEGRADED_RATIO = 0.9
DEGRADED_MIN_COMPANIES = 5

#: §7.4: two consecutive empty runs before removals fire.
EMPTY_SUSPECT = "empty_suspect"
STALE_AFTER_FAILURES = 7

#: §8.6 — no single company may hold the shard hostage.
COMPANY_BUDGET_SECS = 120.0

#: §7.2: the four shards are staggered 25 minutes apart and the per-host token bucket is
#: per-process, so two overlapping shards put 4 rps on every shared ATS host — twice the
#: §5.12 cap the legal posture in §14/§15 rests on. The cost ceiling is not a substitute:
#: the wall clock at which it trips depends on the container's memory tier (60 min at
#: 256 MB), so the stagger needs a guard denominated in seconds.
RUN_DEADLINE_SECS = 20 * 60

#: Container pull + `Actor` init + teardown, billed on every run whatever it does.
#: Measured at ~34 s across the two live runs the cost model was validated against.
CONTAINER_FLOOR_SECS = 34.0

#: §7.7/§7.8: the whole Actor's share of the $5/month credit. The per-run ceiling cannot
#: bound this on its own — nothing in a single run knows how many siblings ran today — so
#: the month-to-date total is carried in `meta.spend` and checked before the sweep starts.
#: Sized to bound 122 runs at :data:`DEFAULTS`'s ceiling (4 x 30.44 x $0.025 = $3.04);
#: the *measured* monthly total is $1.76, so this is a runaway guard, not a quota.
MONTHLY_BUDGET_USD = 3.25

#: §7.7 verified platform prices (R5 §2).
PRICE_CU_USD = 0.20  # 1 CU = 1 GB-hour of compute
PRICE_KV_WRITE_USD = 0.05 / 1000
PRICE_KV_READ_USD = 0.005 / 1000
PRICE_TRANSFER_USD_PER_GB = 0.20

#: Ingress per job of metadata, §7.7's own assumption. `core.http.Client` does not count
#: wire bytes, and transfer is ~3% of a run's cost, so this is estimated rather than
#: measured. ponytail: upgrade to a real byte counter in `Client` if transfer ever
#: becomes the line that trips the ceiling.
BYTES_PER_JOB = 1024

#: Pre-flight sizing. **Measured, not modelled**: a live shard-0 sweep took 348.7 s for
#: 275 companies at `maxConcurrency: 8` and 512 MB — 1.27 s/company, 2.5x the 0.5 s the
#: §7.7 simulation predicted, because the per-host token bucket serialises each provider
#: and four adapters still ship full descriptions. The old value made the preflight pass a
#: shard the ceiling then stopped 5 buckets short, which is exactly the silent partial day
#: §7 exists to prevent. `JOBS_PER_COMPANY` was confirmed by the same run (12,151/275=44).
SECS_PER_COMPANY = 1.3
JOBS_PER_COMPANY = 50


# ------------------------------------------------------------------ input / cost


def _bounded(key: str, value: Any) -> float:
    low, high = NUMERIC_BOUNDS[key]
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = float(DEFAULTS[key])  # type: ignore[arg-type]
    if number != number:  # NaN: every comparison below would be False
        number = float(DEFAULTS[key])  # type: ignore[arg-type]
    return min(high, max(low, number))


def read_config(raw: dict[str, Any]) -> dict[str, Any]:
    cfg = {k: (raw[k] if raw.get(k) is not None else v) for k, v in DEFAULTS.items()}
    for key in NUMERIC_BOUNDS:
        value = _bounded(key, cfg[key])
        cfg[key] = value if key == "costCeilingUsd" else int(value)
    cfg["shard"] = min(cfg["shard"], cfg["shardCount"] - 1)
    cfg["reseedWatchlist"] = bool(cfg["reseedWatchlist"])
    cfg["force"] = bool(cfg["force"])
    cfg["purgeProvider"] = str(cfg["purgeProvider"] or "").strip().lower()
    cfg["purgeCompany"] = str(cfg["purgeCompany"] or "").strip()
    return cfg


@dataclass(slots=True)
class Budget:
    """The hard guard: an estimate of what *this run* costs Apify, checked after every
    bucket. Compute dominates, so the estimate is dominated by a number we measure
    exactly (elapsed wall clock x container memory)."""

    ceiling_usd: float
    memory_gb: float = 0.5
    started: float = field(default_factory=time.monotonic)
    kv_reads: int = 0
    kv_writes: int = 0
    jobs_fetched: int = 0
    tripped: bool = False

    def cost(self, *, elapsed: float | None = None) -> float:
        hours = (time.monotonic() - self.started if elapsed is None else elapsed) / 3600
        return (
            self.memory_gb * hours * PRICE_CU_USD
            + self.kv_writes * PRICE_KV_WRITE_USD
            + self.kv_reads * PRICE_KV_READ_USD
            + self.jobs_fetched * BYTES_PER_JOB / 1e9 * PRICE_TRANSFER_USD_PER_GB
        )

    def estimate(self, companies: int, shard_count: int) -> float:
        """Pre-flight: what this shard costs if it runs to the end. `shard_count` divides
        the wall clock because the shards hit the same rate-limited hosts one at a time."""
        secs = companies * SECS_PER_COMPANY + CONTAINER_FLOOR_SECS
        writes = 2 * BUCKETS / max(1, shard_count) + 2
        return (
            self.cost(elapsed=secs)
            + writes * PRICE_KV_WRITE_USD
            + (companies * JOBS_PER_COMPANY * BYTES_PER_JOB / 1e9 * PRICE_TRANSFER_USD_PER_GB)
        )

    def over(self) -> bool:
        if self.cost() >= self.ceiling_usd:
            self.tripped = True
        return self.tripped


#: Stop reasons that lose part of the day. §7 opens with "every day not collected is
#: permanently lost", so none of them may exit SUCCEEDED — and the 5% failure rule cannot
#: catch them, because the companies in the buckets never reached are not in
#: `companies_done` at all (H1 H1).
LOSSY_STOPS = ("bucket_error", "finalize_error", "budget_ceiling", "run_deadline")


def should_fail(stopped: str | None, companies_done: int, failed: int) -> bool:
    """§7.2: a shard raises when it lost part of its day, turning silent degradation into
    the email the schedule already sends."""
    return stopped in LOSSY_STOPS or bool(companies_done and failed / companies_done > 0.05)


def container_memory_gb() -> float:
    try:
        mb = Actor.get_env().get("memory_mbytes")
    except Exception as exc:  # not on the platform, or the env is not populated yet
        Actor.log.warning(
            "memory tier unknown; billing the guard at 512 MB", extra={"e": repr(exc)}
        )
        mb = None
    if not mb:
        Actor.log.warning("memory tier unknown; billing the guard at 512 MB")
        mb = 512
    return max(0.128, float(mb) / 1024)


# ---------------------------------------------------------------------- watchlist


def watch_key(row: dict[str, Any]) -> str:
    return f"{row['provider']}:{row['company']}"


def seed_rows(directory: Any, today: str) -> list[dict[str, Any]]:
    """§7.6: every directory company validated `ok` with `job_count > 0`, gated on
    `ProviderSpec.retainable` so "disable the adapter" and "stop retaining" are one
    switch (§7.3)."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in getattr(directory, "rows", []):
        provider = row.get("provider")
        slug = row.get("slug")
        if provider not in PROVIDERS or not slug or not retainable(provider):
            continue
        if (row.get("job_count") or 0) <= 0:
            continue
        entry = {
            "provider": provider,
            "company": slug,
            "site": row.get("site"),
            "added": today,
            "source": "seed",
        }
        key = watch_key(entry)
        if key in seen:
            continue
        seen.add(key)
        rows.append(entry)
        if len(rows) >= WATCHLIST_CAP:
            break
    return rows


def retainable(provider: str) -> bool:
    try:
        spec = getattr(get_adapter(provider), "SPEC", None)
    except AdapterNotFound:
        return False
    return bool(getattr(spec, "retainable", True))


async def ensure_watchlist(store: HistoryStore, cfg: dict[str, Any], today: str) -> list[dict]:
    rows = await store.watchlist()
    if rows and not cfg["reseedWatchlist"]:
        return rows
    # No client: the directory is read from the KV cache or the baked seed (§6.6). A shard
    # that starts by pulling 2 MB off a CDN spends ingress on data it already ships with.
    directory = await load_directory(None)
    seeded = seed_rows(directory, today)
    if not seeded:
        Actor.log.warning("directory unavailable; keeping the stored watchlist")
        return rows
    merged = {watch_key(r): r for r in seeded}
    merged.update({watch_key(r): r for r in rows})  # never lose a company already tracked
    rows = list(merged.values())
    await store.put_watchlist(rows)
    Actor.log.info("watchlist seeded", extra={"companies": len(rows), "source": directory.source})
    return rows


# ------------------------------------------------------------------------- fetch


def job_row(record: Any) -> dict[str, Any] | None:
    """One :class:`~core.models.JobRecord` -> the §7.3 stored shape. No description, no
    salary, no recruiter, no PII — id/title/location/dept/url/postedAt and nothing else."""
    jid = record.sourceId or record.id
    if not jid:
        return None
    return {
        "id": str(jid),
        "t": record.title,
        "loc": record.locationRaw,
        "dept": record.department,
        "remote": record.remote,
        "url": record.url,
        "posted": (record.postedAt or "")[:10] or None,
        "updated": (record.updatedAt or "")[:10] or None,
    }


async def call_fetch(module: Any, ref: Ref, client: Any, options: dict[str, Any]) -> list:
    """Adapters differ on whether `options` is positional or keyword-only (§5)."""
    params = inspect.signature(module.fetch).parameters
    if "options" in params:
        result = module.fetch(ref, client, options=options)
    elif len(params) >= 3:
        result = module.fetch(ref, client, options)
    else:
        result = module.fetch(ref, client)
    return list(await result) if inspect.isawaitable(result) else list(result)


async def fetch_company(
    entry: dict[str, Any], client: Any, cfg: dict[str, Any]
) -> tuple[list[dict[str, Any]] | None, str]:
    """Returns `(jobs, status)`. `jobs is None` means "do not touch the stored state"."""
    provider = str(entry["provider"])
    ref = Ref(provider=provider, slug=str(entry["company"]), site=entry.get("site") or None)
    options = {
        # §7.3: metadata only. `listOnly` also stops Rippling's mandatory detail call,
        # which would turn a 50-job board into 51 requests and triple the request budget.
        "includeDescription": False,
        "includeRawJson": False,
        "redactContacts": True,
        "listOnly": True,
        "outputProfile": "minimal",
        "deadline": time.monotonic() + COMPANY_BUDGET_SECS,
    }
    try:
        module = get_adapter(provider)
    except AdapterNotFound:
        return None, "provider_unavailable"
    try:
        records = await asyncio.wait_for(
            call_fetch(module, ref, client, options), timeout=COMPANY_BUDGET_SECS
        )
    except FetchError as exc:
        return None, exc.status
    except TimeoutError:
        return None, "timeout"
    except Exception as exc:  # one bad company never ends the shard (§13.4 #14)
        Actor.log.warning("fetch failed", extra={"company": watch_key(entry), "error": repr(exc)})
        return None, "parse_error"
    rows = [row for row in (job_row(r) for r in records) if row]
    return rows, "ok"


# -------------------------------------------------------------------- run context


@dataclass(slots=True)
class Run:
    cfg: dict[str, Any]
    store: HistoryStore
    budget: Budget
    today: str
    events: list[dict[str, Any]] = field(default_factory=list)
    counts: list[dict[str, Any]] = field(default_factory=list)
    statuses: Counter = field(default_factory=Counter)
    empty: Counter = field(default_factory=Counter)
    seen_per_provider: Counter = field(default_factory=Counter)
    events_written: int = 0
    events_resumed: int = 0
    companies_done: int = 0
    buckets_done: int = 0
    buckets_skipped: int = 0
    stopped: str | None = None


def is_degraded(run: Run, provider: str) -> bool:
    """§5.12/§7.4: a provider answering 200-with-zero-jobs for >90% of its companies is
    broken, not empty — no state updates and no events for it.

    ponytail: the tally is the run so far, not the whole shard, because state is written
    per bucket for resumability. The `empty_suspect` rule already blocks every removal
    from a single empty response, so the earlier buckets lose nothing either way.
    """
    seen = run.seen_per_provider[provider]
    return seen >= DEGRADED_MIN_COMPANIES and run.empty[provider] / seen > DEGRADED_RATIO


async def process_bucket(bucket: int, entries: list[dict], client: Any, run: Run) -> None:
    state = await run.store.state(bucket)
    run.budget.kv_reads = run.store.reads
    if state.get("as_of") == run.today and not run.cfg["force"]:
        run.buckets_skipped += 1  # already collected today: this is a resumed run
        return

    companies = state.get("companies") or {}
    semaphore = asyncio.Semaphore(run.cfg["maxConcurrency"])

    async def one(entry: dict[str, Any]) -> tuple[dict[str, Any], list | None, str]:
        async with semaphore:
            jobs, status = await fetch_company(entry, client, run.cfg)
            return entry, jobs, status

    results = await asyncio.gather(*(one(e) for e in entries))

    for entry, jobs, status in results:
        provider = str(entry["provider"])
        run.seen_per_provider[provider] += 1
        if status == "ok" and not jobs:
            run.empty[provider] += 1

    for entry, jobs, status in results:
        key = watch_key(entry)
        provider = str(entry["provider"])
        prev = companies.get(key) or {}
        prev_jobs = prev.get("jobs") or {}
        run.companies_done += 1
        run.statuses[status] += 1

        if jobs is not None and is_degraded(run, provider):
            # §7.4 row 3: **no** state updates and no events for a degraded provider.
            # Writing `consecutive_failures` here flagged every company of that provider
            # `stale` after seven degraded days, which §7.5 uses to drop companies from
            # the velocity metrics (H1 L8).
            continue

        if jobs is None:
            # §7.4 row 1: keep the previous state untouched, count the failure, emit
            # nothing. A network blip must never look like a mass layoff.
            failures = int(prev.get("consecutive_failures") or 0) + 1
            companies[key] = {
                **prev,
                "status": status,
                "consecutive_failures": failures,
                "tracked_since": prev.get("tracked_since") or run.today,
                "jobs": prev_jobs,
                "stale": failures >= STALE_AFTER_FAILURES,
            }
            continue

        suspect = bool(jobs == [] and prev_jobs)
        if suspect and prev.get("status") != EMPTY_SUSPECT:
            # §7.4 row 2: two consecutive empty runs are required before removals fire.
            companies[key] = {
                **prev,
                "status": EMPTY_SUSPECT,
                "consecutive_failures": 0,
                "tracked_since": prev.get("tracked_since") or run.today,
                "jobs": prev_jobs,
            }
            run.statuses[EMPTY_SUSPECT] += 1
            continue

        nxt, events = diff(prev_jobs, jobs, run.today, provider, str(entry["company"]))
        companies[key] = {
            "status": "ok",
            "consecutive_failures": 0,
            "tracked_since": prev.get("tracked_since") or run.today,
            "jobs": nxt,
        }
        run.events.extend(events)
        run.counts.append(
            {"d": run.today, "provider": provider, "company": entry["company"], "open": len(nxt)}
        )

    # Events are the one thing that cannot be recomputed from state, so they are flushed
    # with the bucket that produced them rather than at the end of the run (§7: a lost day
    # is lost forever) — and **before** the state write, not after: a `put_events` that
    # raised once `state.as_of` was already today left tomorrow's diff seeing those jobs in
    # `prev`, so the `added` events were never re-emitted (H1 M1). `counts` and `meta` are
    # derivable and wait for the end.
    if len(run.events) != run.events_written:
        # `put_events` rewrites the whole value, so re-writing an unchanged list once per
        # bucket cost 16 writes/shard for one bucket's worth of information (H1 M2).
        await run.store.put_events(run.today, run.cfg["shard"], run.events)
        run.events_written = len(run.events)
    state["as_of"] = run.today
    state["companies"] = companies
    await run.store.put_state(bucket, state)
    run.buckets_done += 1
    run.budget.kv_reads, run.budget.kv_writes = run.store.reads, run.store.writes
    run.budget.jobs_fetched += sum(len(j) for _, j, _ in results if j)


async def resume_events(run: Run) -> None:
    """Carry today's already-written events into this run before anything flushes.

    `put_events` rewrites the whole `events.{date}.{shard}` value, so a restarted run that
    started with an empty list would overwrite the morning's events with only its own —
    measured on a live `force` run that wiped 1,720 `added` events. This is the one thing
    §7 says cannot be recovered, so it is loaded before the first bucket, not after.
    """
    run.events.extend(await run.store.events(run.today, run.cfg["shard"]))
    # This run did not produce them; counting them again inflates `meta.events`, a §14.1
    # monitoring number, every time a run restarts (H1 L6).
    run.events_resumed = run.events_written = len(run.events)


async def finalize(run: Run) -> None:
    """counts.{day}.{shard} and meta. Both are idempotent for the day."""
    if run.counts:
        # Write-once, no read-modify-write: keyed per day and shard there is nothing to
        # merge, no month-sized value to materialise, and no lost update when two shards
        # overlap (H1 M4/M5).
        await run.store.put_counts(run.today, run.cfg["shard"], run.counts)

    meta = await run.store.meta()
    last_run = meta.get("last_run") if isinstance(meta.get("last_run"), dict) else {}
    last_run[str(run.cfg["shard"])] = run.today
    tally = (
        {
            "ok": run.statuses["ok"],
            "failed": run.companies_done - run.statuses["ok"],
            "companies": run.companies_done,
            "events": len(run.events) - run.events_resumed,
        }
        # A resumed run swept nothing, so reporting its zeros would erase the real run's
        # summary. `last_run` still advances, because the day *is* collected.
        if run.companies_done
        else {}
    )
    # §7.7/§7.8 month-to-date: only the current month is kept, so `meta` stays a handful
    # of bytes and the accumulator cannot itself become the thing that grows.
    month = run.today[:7]
    prior = meta.get("spend") if isinstance(meta.get("spend"), dict) else {}
    spend = {month: round(float(prior.get(month) or 0.0) + run.budget.cost(), 6)}
    await run.store.put_meta(
        {**meta, "version": VERSION, "last_run": last_run, "spend": spend, **tally}
    )
    run.budget.kv_reads, run.budget.kv_writes = run.store.reads, run.store.writes


# ------------------------------------------------------------------------- entry


async def main() -> None:
    async with Actor:
        cfg = read_config(await Actor.get_input() or {})
        today = datetime.now(UTC).date().isoformat()
        budget = Budget(ceiling_usd=cfg["costCeilingUsd"], memory_gb=container_memory_gb())

        store = await HistoryStore.open(STORE_NAME)
        run = Run(cfg=cfg, store=store, budget=budget, today=today)

        if cfg["purgeProvider"]:
            # §15.1 policy 4 promises a takedown inside 48 hours. `HistoryStore.purge` had
            # no caller anywhere in the repo, so that promise had no executable path (H1
            # H3); this is it.
            removed = await store.purge(
                cfg["purgeProvider"],
                cfg["purgeCompany"] or None,
                keys=await store.keys("events."),
            )
            line = f"purged {cfg['purgeProvider']}:{cfg['purgeCompany'] or '*'} -> {removed}"
            Actor.log.info(line)
            await Actor.set_status_message(line)
            return

        await resume_events(run)
        watchlist = await ensure_watchlist(store, cfg, today)
        mine = [
            row
            for row in watchlist
            if bucket_of(str(row["provider"]), str(row["company"]))
            in set(buckets_for_shard(cfg["shard"], cfg["shardCount"]))
        ]
        # §7.3: `retainable` is the one switch for "disable the adapter" and "stop
        # retaining". Gating only `seed_rows` left every company already on the watchlist
        # collected forever, because `ensure_watchlist` returns the stored rows unfiltered
        # and even a reseed merges them back in (H1 H3). The choke point is here.
        mine = [row for row in mine if retainable(str(row["provider"]))]
        if cfg["maxCompanies"]:
            mine = mine[: cfg["maxCompanies"]]

        mtd = float(((await store.meta()).get("spend") or {}).get(today[:7]) or 0.0)
        estimate = budget.estimate(len(mine), cfg["shardCount"])
        if mtd + estimate > MONTHLY_BUDGET_USD:
            await Actor.fail(
                status_message=(
                    f"month-to-date ${mtd:.4f} + ${estimate:.4f} would pass this Actor's "
                    f"${MONTHLY_BUDGET_USD:.2f} share of the monthly credit (§7.7/§7.8)"
                )
            )
            return
        if estimate > cfg["costCeilingUsd"]:
            # Hard guard: refuse to start rather than discover the overspend afterwards.
            await Actor.fail(
                status_message=(
                    f"estimated ${estimate:.4f} for {len(mine)} companies exceeds the "
                    f"${cfg['costCeilingUsd']:.4f} ceiling; raise costCeilingUsd, "
                    f"lower maxCompanies, or add shards (§7.8)"
                )
            )
            return
        if not mine:
            Actor.log.warning("shard is empty", extra={"shard": cfg["shard"]})
            return

        by_bucket: dict[int, list[dict[str, Any]]] = {}
        for row in mine:
            by_bucket.setdefault(bucket_of(str(row["provider"]), str(row["company"])), []).append(
                row
            )

        Actor.log.info(
            "snapshot start",
            extra={
                "shard": cfg["shard"],
                "shardCount": cfg["shardCount"],
                "companies": len(mine),
                "buckets": len(by_bucket),
                "estimateUsd": round(estimate, 5),
                "ceilingUsd": cfg["costCeilingUsd"],
            },
        )

        async def on_abort(_data: Any = None) -> None:
            # 30 s window: the day's events for finished buckets are already flushed, so
            # only the derivable tail needs saving.
            await finalize(run)

        Actor.on(Event.ABORTING, on_abort)
        Actor.on(Event.MIGRATING, on_abort)

        async with make_client(
            timeout_secs=cfg["requestTimeoutSecs"],
            max_connections=cfg["maxConcurrency"],
        ) as client:
            for bucket in sorted(by_bucket):
                overdue = time.monotonic() - budget.started > RUN_DEADLINE_SECS
                if budget.over() or overdue:
                    run.stopped = (
                        "run_deadline" if overdue and not budget.tripped else ("budget_ceiling")
                    )
                    Actor.log.error(
                        "stopping early with the collected buckets saved",
                        extra={
                            "why": run.stopped,
                            "costUsd": round(budget.cost(), 5),
                            "elapsedSecs": round(time.monotonic() - budget.started),
                            "bucket": bucket,
                        },
                    )
                    break
                try:
                    await process_bucket(bucket, by_bucket[bucket], client, run)
                except Exception:
                    # One bucket must never cost the other fifteen (§7.2).
                    Actor.log.exception("bucket failed", extra={"bucket": bucket})
                    run.stopped = run.stopped or "bucket_error"

        try:
            await finalize(run)
        except Exception:
            # `counts` and `meta` are derivable from state; losing them must not cost the
            # one log line that says what the run actually collected.
            Actor.log.exception("finalize failed")
            run.stopped = run.stopped or "finalize_error"

        added = sum(1 for e in run.events if e["ev"] == "added")
        removed = sum(1 for e in run.events if e["ev"] == "removed")
        changed = len(run.events) - added - removed
        failed = run.companies_done - run.statuses["ok"]
        line = (
            f"shard={cfg['shard']}/{cfg['shardCount']} d={today} "
            f"buckets={run.buckets_done}(+{run.buckets_skipped} done) "
            f"companies={run.companies_done} ok={run.statuses['ok']} failed={failed} "
            f"events={len(run.events)} (+{added}/-{removed}/~{changed}) "
            f"open={sum(c['open'] for c in run.counts)} "
            f"kv={store.writes}w/{store.reads}r bytes={store.bytes_written} "
            f"cost~${budget.cost():.5f} stopped={run.stopped or '-'}"
        )
        Actor.log.info(line)
        await Actor.set_status_message(line)

        if should_fail(run.stopped, run.companies_done, failed):
            await Actor.fail(status_message=line)


if __name__ == "__main__":
    asyncio.run(main())
