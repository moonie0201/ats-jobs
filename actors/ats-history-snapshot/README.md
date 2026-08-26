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
| `costCeilingUsd` | `0.05` | hard budget guard, per run |
| `maxConcurrency` | `8` | parallel companies inside a bucket |
| `requestTimeoutSecs` | `30` | per request |
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

## What is stored (§7.3) — and what is not

Per job: `id`, `t`itle, `loc`ation, `dept`, `remote`, `url`, `posted`, `updated`,
`first_seen`, `last_seen`, and `h` — an 8-hex hash of (title, location, department,
remote) so `changed` means a *material* change.

**Never stored:** descriptions, salaries, recruiter names, contacts, any PII. That keeps
each job at ~220 B instead of ~7.7 KB and keeps the store inside the free tier.

Rippling is fetched **list-only**: its detail call is mandatory even without descriptions,
so a 50-job board would be 51 requests. `employment_type` and `createdOn` are null for
Rippling here, by design.

## Takedown

`core.history.HistoryStore.purge(provider, company=None, keys=[...])` drops a company (or
a whole provider) from every state bucket and rewrites the affected event files — §15.1
policy 4 promises 48 hours, and disabling an adapter only stops *collection*.

## Cost (§7.7)

≈$1.63/month for the whole 4-shard, 5,000-company design, out of a $5/month free credit.
A single shard is ≈$0.013. `costCeilingUsd` defaults to ~4× that.

## Local run

```bash
python scripts/sync_actor_files.py ats-history-snapshot
cd actors/ats-history-snapshot && apify run --input '{"shard":0,"maxCompanies":20}'
cd - && python scripts/sync_actor_files.py --clean ats-history-snapshot
```
