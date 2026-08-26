#!/usr/bin/env python3
"""The four daily `ats-history-snapshot` Schedules, as code (SPEC v2 §7.2).

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

#: §7.2's stagger. Four shards, 25 minutes apart, UTC. Keep them apart by more than
#: `RUN_DEADLINE_SECS` (20 min) or the guarantee above stops holding.
CRONS = {0: "0 3 * * *", 1: "25 3 * * *", 2: "50 3 * * *", 3: "15 4 * * *"}

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
        "title": f"ATS history snapshot - shard {shard}/4",
        "cron_expression": cron,
        "timezone": "UTC",
        "is_enabled": True,
        # One run of a shard at a time: a schedule that fires while yesterday's run is
        # still going would double the request rate on every host it shares.
        "is_exclusive": True,
        "description": (
            f"SPEC v2 7.2: sweeps buckets {shard * 16:02d}-{shard * 16 + 15:02d} of the "
            "watchlist daily, staggered 25 min after the previous shard so the global "
            "per-host rate stays at 5.12's 2 rps. Memory is pinned at 512 MB because the "
            "budget guard bills compute at the tier it is told."
        ),
        "actions": [
            {
                "type": "RUN_ACTOR",
                "actorId": ACTOR_ID,
                "runInput": {
                    "body": json.dumps({"shard": shard}),
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
