#!/usr/bin/env python3
"""D14 KPI table for the `ats-jobs-scraper` Actor (SPEC v2 §14.5, §16.2).

Reads the public Actor object (no auth) for store-wide stats, and the developer's
own run list (auth) for platform cost and charged-event shape.

Auth: `APIFY_TOKEN` if set, else the logged-in `apify` CLI (which keeps its token
in the OS keyring, so this script never touches the secret itself).

    python scripts/kpi.py             # KPI table + verdict
    ATS_D0=2026-08-26 python scripts/kpi.py
    python scripts/kpi.py --selftest  # verdict truth table

What is NOT observable, and is therefore estimated (§16.2, V3 M3): an Actor
developer cannot enumerate other accounts' runs, inputs or charged events. Only
aggregate `stats` is exposed. Rows marked `~` are derived, not measured.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime

ACTOR = "aZfd1nEuYfHNDz2mt"
API = "https://api.apify.com/v2"
# §16.2 decision function + the D14 gate this script is asked to report on.
GO_PAID_RUNS, GO_ACCOUNTS, KILL_RUNS, KILL_USERS_PER_DAY = 10, 3, 30, 0.7


def get(path: str, token: str | None) -> dict:
    req = urllib.request.Request(f"{API}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["data"]


def cli(*args: str) -> dict:
    out = subprocess.run(["apify", *args, "--json"], capture_output=True, text=True, timeout=60)
    if out.returncode:
        sys.exit(f"apify {' '.join(args)} failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


def own_runs(token: str | None) -> list[dict]:
    """The developer's own runs. Other accounts' runs are not enumerable."""
    if token:
        return get(f"/acts/{ACTOR}/runs?limit=1000", token)["items"]
    return cli("runs", "ls", ACTOR, "--limit", "1000")["items"]


def run_detail(run_id: str, token: str | None) -> dict:
    return get(f"/actor-runs/{run_id}", token) if token else cli("runs", "info", run_id)


def verdict(paid_runs: int, accounts: int, total_runs: int, users_per_day: float) -> str:
    """Task gate first (GO), then §16.2's KILL floors, else HOLD."""
    if paid_runs >= GO_PAID_RUNS and accounts >= GO_ACCOUNTS:
        return "GO"
    if total_runs < KILL_RUNS or users_per_day < KILL_USERS_PER_DAY:
        return "KILL"
    return "HOLD"


def selftest() -> None:
    assert verdict(10, 3, 100, 2.0) == "GO"
    assert verdict(10, 3, 0, 0.0) == "GO", "GO clause wins over the KILL floors"
    assert verdict(9, 3, 100, 2.0) == "HOLD"
    assert verdict(10, 2, 100, 2.0) == "HOLD"
    assert verdict(0, 0, 29, 2.0) == "KILL"
    assert verdict(0, 0, 100, 0.69) == "KILL"
    assert verdict(0, 0, 30, 0.7) == "HOLD"
    print("selftest ok")


def main() -> None:
    if "--selftest" in sys.argv:
        return selftest()

    token = os.environ.get("APIFY_TOKEN")
    actor = get(f"/acts/{ACTOR}", token)
    stats, events = actor["stats"], {}
    for info in actor.get("pricingInfos") or []:
        events = info.get("pricingPerEvent", {}).get("actorChargeEvents", events)
    price = {k: v.get("eventPriceUsd", 0.0) for k, v in events.items()}

    d0 = datetime.fromisoformat(
        (os.environ.get("ATS_D0") or actor["createdAt"]).replace("Z", "+00:00")
    )
    if d0.tzinfo is None:
        d0 = d0.replace(tzinfo=UTC)
    days = max((datetime.now(UTC) - d0).total_seconds() / 86400, 1e-9)

    runs = own_runs(token)
    details = [run_detail(r["id"], token) for r in runs]
    failed = sum(1 for r in details if r["status"] != "SUCCEEDED")
    platform_cost = sum(r.get("usageTotalUsd") or 0.0 for r in runs)
    dev_events: dict[str, int] = {}
    for r in details:
        for name, n in (r.get("chargedEventCounts") or {}).items():
            dev_events[name] = dev_events.get(name, 0) + n

    total_runs, users = stats["totalRuns"], stats["totalUsers"]
    paid_runs = max(total_runs - len(runs), 0)  # runs billed to another account
    # median, not mean: one 775-job backfill must not set the estimator for everyone
    per_run = [(r.get("chargedEventCounts") or {}).get("job", 0) for r in details]
    jobs_per_run = statistics.median(per_run) if per_run else 0.0
    est_jobs = round(paid_runs * jobs_per_run)
    revenue = est_jobs * price.get("job", 0.0) + paid_runs * price.get("apify-actor-start", 0.0)
    margin = 0.8 * revenue - platform_cost  # §8.1: profit = 0.8 x revenue - cost

    def row(k: str, v: object, note: str = "") -> None:
        print(f"  {k:<38} {str(v):>14}  {note}")

    print(f"\nKPI  {actor['username']}/{actor['name']}  ({ACTOR})")
    print(f"     https://apify.com/{actor['username']}/{actor['name']}")
    print(f"     D0 {d0:%Y-%m-%d} -> D{int(days)} of 14        {'-' * 28}")
    print("\n  ACQUISITION")
    row("total users", users)
    row("users 7d / 30d", f"{stats['totalUsers7Days']} / {stats['totalUsers30Days']}")
    row("users/day since D0", f"{users / days:.2f}", f"KILL <{KILL_USERS_PER_DAY}")
    print("\n  USAGE")
    row("total runs (all accounts)", total_runs, f"KILL <{KILL_RUNS}")
    row("runs by the developer", len(runs), "not revenue")
    row("runs by other accounts", paid_runs, f"GO >={GO_PAID_RUNS}")
    row("distinct users seen in runs", len({r["userId"] for r in runs}), "own account only")
    row("failed runs (own)", f"{failed}/{len(runs)}", "GO <2% (§16.2)")
    print("\n  MONETIZATION")
    for name, n in sorted(dev_events.items()):
        row(f"~ {name} events (dev runs)", n, f"@ ${price.get(name, 0):g}")
    row("~ jobs/run (dev median)", f"{jobs_per_run:.1f}", "estimator basis")
    row("~ charged `job` events", est_jobs, "extrapolated, not measured")
    row("~ revenue estimate", f"${revenue:.4f}", "GO >$0")
    row("platform cost (own runs)", f"${platform_cost:.4f}", "other runs bill the user")
    row("~ margin (0.8*rev - cost)", f"${margin:.4f}")
    print("\n  VERDICT")
    v = verdict(paid_runs, users, total_runs, users / days)
    stage = "FINAL" if days >= 14 else f"PROVISIONAL (D{int(days)}, decide at D14)"
    row(v, stage)
    print(
        f"\n  GO = other-account runs >={GO_PAID_RUNS} AND accounts >={GO_ACCOUNTS};"
        f" KILL = runs <{KILL_RUNS} OR users/day <{KILL_USERS_PER_DAY}; else HOLD."
    )
    print(
        "  `~` rows are derived: other accounts' charged events are not readable"
        " by an Actor developer (§16.2).\n"
    )


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        sys.exit(f"Apify API {e.code}: {e.reason} — set APIFY_TOKEN or run `apify login`")
    except FileNotFoundError:
        sys.exit("no APIFY_TOKEN and no `apify` CLI on PATH")
