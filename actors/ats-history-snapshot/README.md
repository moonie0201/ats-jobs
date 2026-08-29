# ats-history-snapshot (B1) — internal

**Private. Never publish to Apify Store.** This Actor sells nothing, pushes no dataset
rows and charges no PPE events. It exists to collect the one asset that cannot be
backfilled: a daily record of which jobs were open at which company (SPEC v2 §7).

A competitor who clones this repo on day 400 still has zero days of history.

## What one run does

```
watchlist  ──► buckets owned by this shard ──► adapter fetch (metadata only)
                                                     │
                        core.diff (§7.4) ◄───────────┘
                                │
       state/{bucket}  +  events/{date}/{shard}   (flushed per bucket)
                                │
                     counts/{YYYY-MM}  +  meta    (at the end)
```

Everything lands in the **named** key-value store `ats-history`. Named stores are never
deleted by retention on any plan — that is the whole reason for the name.

## Input

| field | default | meaning |
|---|---|---|
| `shard` | `0` | which slice of the 64 buckets this run owns (0 → 00-15, 1 → 16-31, …) |
| `shardCount` | `4` | how many shards the watchlist is split across |
| `maxCompanies` | `0` | cap for smoke runs; 0 = the whole shard |
| `costCeilingUsd` | `0.04` | hard budget guard, per run |
| `maxConcurrency` | `8` | parallel companies inside a bucket |
| `requestTimeoutSecs` | `30` | per request |
| `maxVerifyRequests` | `1000` | cap on the single-posting GETs that confirm Greenhouse/Lever removals, per run |
| `reseedWatchlist` | `false` | merge the directory back into the watchlist first |
| `force` | `false` | re-diff buckets already collected today |

## Schedules (§7.2)

Four schedules, one per shard, **staggered 25 minutes apart**, 512 MB, 90-minute timeout:

| shard | buckets | cron (UTC) |
|---|---|---|
| 0 | 00-15 | `0 3 * * *` |
| 1 | 16-31 | `25 3 * * *` |
| 2 | 32-47 | `50 3 * * *` |
| 3 | 48-63 | `15 4 * * *` |

Staggering is load-bearing, not cosmetic. Four concurrent runs would put 8 rps on
`boards-api.greenhouse.io` and break the politeness cap the legal posture rests on.

## The four guards

1. **Budget.** A pre-flight estimate refuses to start when the run would cost more than
   `costCeilingUsd`; a running estimate (measured wall clock × container memory, plus
   counted KV ops) stops the sweep early and still saves every bucket collected so far.
2. **Rate limiting.** Per-host token buckets in `core.http` — 2 rps, 1 rps for Lever —
   shared by every company on that host, because four providers are one host each.
3. **Resumability.** `state/{bucket}.as_of == today` means the bucket is done; a restarted
   run skips it. Events are flushed with the bucket that produced them, so a timeout costs
   only the buckets not yet reached.
4. **No false layoffs.** A failed fetch leaves the previous state untouched and emits no
   events. A 200 with zero jobs where jobs existed is marked `empty_suspect` and needs a
   second consecutive empty run before removals fire. A provider answering
   200-with-zero-jobs for >90% of its companies is marked degraded and updates nothing.
   A Greenhouse or Lever `removed` is confirmed against the provider's single-posting
   endpoint before it is recorded: 404 → `verified: true`; 200 → no event, the job stays
   open and is re-checked next sweep; unreachable → no event until three consecutive
   sweeps, then `verified: false`. Only a literal 404 counts as gone — a 3xx from the
   endpoint is "unreachable", never a closure. A board whose feed went empty stays
   `empty_suspect` for as long as unverified jobs remain in its state, so it is asked on
   every sweep and "three consecutive sweeps" holds for an ATS migration too. Ashby,
   Recruitee, Rippling and Personio have no such endpoint, so their removals carry
   `verified: null` — the feed is the only signal.
   The verify phase is sequential and rate-limited (2 rps Greenhouse, 1 rps Lever); each
   ask gets one request timeout, and a run already past `costCeilingUsd` or the 20-minute
   deadline stops asking and leaves the rest for the next sweep. `maxVerifyRequests`
   (1,000) covers a layoff day: 2026-08-28 removed 1,120 jobs across four shards. A shard
   that hits the cap on consecutive days records its overflow `verified: false` two days
   late; the cap is a runaway guard, not the expected path. Measured 2026-08-29 (run
   `gydyIo2aYH8j5mWUe`, 385 companies, `/departments` on): 784 s, $0.0265 by the guard,
   $0.023 billed — hence the $0.04 ceiling.

## What is stored (§7.3) — and what is not

Per job: `id`, `t`itle, `loc`ation, `dept`, `remote`, `url`, `posted`, `updated`,
`first_seen`, `last_seen`, and `h` — an 8-hex hash of (title, location, department,
remote) so `changed` means a *material* change.

**Never stored:** descriptions, salaries, recruiter names, contacts, any PII. That keeps
each job at ~220 B instead of ~7.7 KB and keeps the store inside the free tier.

Rippling is fetched **list-only**: its detail call is mandatory even without descriptions,
so a 50-job board would be 51 requests. `employment_type` and `createdOn` are null for
Rippling here, by design.

Greenhouse is fetched list-only too, and without `content=true` its board API silently
drops `departments` from every job — so `dept` was null on every Greenhouse row until
2026-08-29. Since then the adapter fetches `GET /boards/{slug}/departments` as a second
request per board (about the size of the list itself; Stripe 469 KB vs 4.48 MB with
`content=true`) and names each job from it. If that call fails the board still stands
with `dept` null for the day — the call is asked once, never retried, so a 429 with a
`Retry-After` cannot outlast the company budget and cost the board day the list call
already paid for. `core.diff` treats a null on *either* side of `dept` as our data gap,
not the employer's edit: neither the one-off backfill nor a departments outage emits
`changed: ["dept"]` for a whole board, and a null from a failed `/departments` call never
overwrites a department already in state — a `removed` on a later day still carries it.
Only that day's `added` rows go out with `dept` null. The §7.5 repost rule likewise
ignores `dept` when either side is null, so a repost across such a day is not billed as
a fill.

## Retention

`core.history.HistoryStore.prune(today)` runs inside `finalize()` on **every** run and
deletes every `events.{day}.{shard}` and `counts.{day}.{shard}` older than
`RETENTION_DAYS` (400). State buckets are not swept — they hold one row per live company,
not a growing history. 400 days is over a year, so year-on-year comparisons still work, and
it is finite, which is the point: `PRIVACY.md` publishes this number and "indefinite" is not
a retention period anyone can defend.

Deletion is `set_value(key, None)`, the key-value store's own remove. Not on a separate
schedule: a sweep that needs its own schedule is a sweep that silently stops running.

## Takedown

`core.history.HistoryStore.purge(provider, company=None, keys=[...])` drops a company (or
a whole provider) from every state bucket and rewrites the affected event files. Disabling
an adapter only stops *collection*, so this is the thing that actually removes what is
already stored. The public promise is 48 hours — see `TAKEDOWN.md` in the repository root
for the route people use to invoke it.

## Cost (§7.7)

≈$1.63/month for the whole 4-shard, 5,000-company design, out of a $5/month free credit.
A single shard is ≈$0.013. `costCeilingUsd` defaults to ~4× that.

## Local run

```bash
python scripts/sync_actor_files.py ats-history-snapshot
cd actors/ats-history-snapshot && apify run --input '{"shard":0,"maxCompanies":20}'
cd - && python scripts/sync_actor_files.py --clean ats-history-snapshot
```
