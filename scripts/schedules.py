#!/usr/bin/env python3
"""The daily `ats-history-snapshot` Schedules, as code (SPEC v2 §7.2).

The 25-minute stagger is load-bearing and it used to live only in the Apify Console,
where one edit silently breaks §5.12: the per-host token bucket in `core.http` is per
*process*, so two shards running at once put 4 rps on every shared ATS host instead of 2.
`RUN_DEADLINE_SECS` in the runner is the other half of that guarantee — it stops a shard
before it can outlive its stagger. This script is the half that lives in git.

Idempotent: run it again after changing a cron or the memory tier and it updates in
place. It never deletes a schedule it did not create.

    APIFY_TOKEN="$(apify auth token)" python scripts/schedules.py
    APIFY_TOKEN="$(apify auth token)" python scripts/schedules.py --dry-run
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from apify_client import ApifyClient

ACTOR_ID = "SMtppr38BwH4VJySF"  # acotr_moonie/ats-history-snapshot

#: §7.2's stagger. Shards run 25 minutes apart, UTC, and must not overlap.
#:
#: A shard is a whole number of the 64 buckets, so SHARD_COUNT has to divide 64 or the
#: three-bucket shards overrun the stagger while the two-bucket ones idle. Measured on
#: 2026-09-04 against the 14,243-board watchlist: 936 companies (4 buckets, 1/16th) took
#: 1,532 s and $0.044 — past the 1,500 s stagger. Two buckets is ~445 companies and
#: ~730 s, which clears both the stagger and `RUN_DEADLINE_SECS` (1,200 s) with room for
#: a slow provider day.
SHARD_COUNT = 32
STAGGER_MIN = 25
FIRST_HOUR = 3  # UTC; the sweep then runs to ~16:20


def _cron(shard: int) -> str:
    """`shard` minutes-from-first-run, as a daily cron. 32 shards x 25 min = 13h20m."""
    total = FIRST_HOUR * 60 + shard * STAGGER_MIN
    return f"{total % 60} {(total // 60) % 24} * * *"


CRONS = {shard: _cron(shard) for shard in range(SHARD_COUNT)}

#: §7.1 / H1 L4: the budget guard bills compute at the tier it is told about, so an
#: unpinned schedule makes every cost number in the run log wrong by 2x.
MEMORY_MBYTES = 512
TIMEOUT_SECS = 3600


def _get(obj: Any, *names: str) -> Any:
    """The client returns dicts on some paths and model objects on others."""
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def payload(shard: int, cron: str) -> dict[str, Any]:
    return {
        "name": f"ats-history-snapshot-shard-{shard}",
        "title": f"ATS history snapshot - shard {shard}/{SHARD_COUNT}",
        "cron_expression": cron,
        "timezone": "UTC",
        "is_enabled": True,
        # One run of a shard at a time: a schedule that fires while yesterday's run is
        # still going would double the request rate on every host it shares.
        "is_exclusive": True,
        "description": (
            f"SPEC v2 7.2: sweeps {64 // SHARD_COUNT} of the 64 watchlist buckets daily, "
            f"staggered {STAGGER_MIN} min after the previous shard so the global per-host "
            "rate stays at 5.12's 2 rps. Memory is pinned at 512 MB because the budget "
            "guard bills compute at the tier it is told."
        ),
        "actions": [
            {
                "type": "RUN_ACTOR",
                "actorId": ACTOR_ID,
                "runInput": {
                    "body": json.dumps({"shard": shard, "shardCount": SHARD_COUNT}),
                    "contentType": "application/json; charset=utf-8",
                },
                "runOptions": {
                    "build": "latest",
                    "memoryMbytes": MEMORY_MBYTES,
                    "timeoutSecs": TIMEOUT_SECS,
                },
            }
        ],
    }


def main() -> int:
    dry = "--dry-run" in sys.argv
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print('set APIFY_TOKEN, e.g. APIFY_TOKEN="$(apify auth token)"', file=sys.stderr)
        return 1
    client = ApifyClient(token)
    existing = {_get(s, "name"): s for s in client.schedules().list().items}

    for shard, cron in CRONS.items():
        body = payload(shard, cron)
        name = body["name"]
        if dry:
            print(f"would {'update' if name in existing else 'create'} {name} @ {cron}")
            continue
        if name in existing:
            got = client.schedule(_get(existing[name], "id")).update(**body)
            verb = "updated"
        else:
            got = client.schedules().create(**body)
            verb = "created"
        print(
            f"{verb} {_get(got, 'id')} {_get(got, 'name')} "
            f"cron={cron!r} next={_get(got, 'next_run_at', 'nextRunAt')}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
