#!/usr/bin/env python3
"""Merge the public ATS company roster into the watchlist, keeping only live boards.

The watchlist was 1,574 hand-seeded boards. `kalil0321/ats-scrapers` publishes a company
roster for 47 ATS platforms under MIT, six of which we already read, and a slug is a fact
about which board a company publishes on — we still fetch every posting from the provider
ourselves, so this is discovery, not redistribution.

Every candidate is probed once from a laptop before it is added. A board that 404s today
will not start answering because Apify polled it 1,400 more times, and each dead slug in
the watchlist is a daily platform charge forever. The sample said 75% carry postings;
probing the real thing is cheaper than being wrong about it.

Usage:
    python scripts/expand_watchlist.py --probed /tmp/roster/probed.jsonl \\
        --existing /tmp/roster/existing.jsonl --out /tmp/roster/watchlist.json [--apply]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import urllib.request

STORE_ID = "F97QIaKY3NR1xJ58y"
TODAY = "2026-09-04"

# A board answering 200 with zero postings is a real company that is not hiring today,
# and `open: 0` is a fact worth recording. But we are paying per board per day and an
# empty board has never been seen to carry anything, so it does not earn its slot on
# first sighting. Boards that had postings when probed are the ones we buy.
MIN_JOBS = 1


def load(path: str) -> list[dict]:
    return [
        json.loads(line) for line in pathlib.Path(path).read_text().splitlines() if line.strip()
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probed", required=True)
    ap.add_argument("--existing", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--apply", action="store_true", help="write the merged list to the KV store")
    args = ap.parse_args()

    existing = load(args.existing)
    have = {(e["provider"], e["company"]) for e in existing}
    probed = load(args.probed)

    kept, empty, dead = [], 0, 0
    for r in probed:
        key = (r["provider"], r["company"])
        if key in have:
            continue
        if r["status"] != "ok":
            dead += 1
            continue
        if r["jobs"] < MIN_JOBS:
            empty += 1
            continue
        have.add(key)
        kept.append(
            {
                "provider": r["provider"],
                "company": r["company"],
                "site": None,
                "added": TODAY,
                "source": "jobhive-roster",
            }
        )

    merged = existing + kept
    pathlib.Path(args.out).write_text(json.dumps(merged, ensure_ascii=False))

    print(f"existing {len(existing)}  probed {len(probed)}")
    print(f"  kept   {len(kept)}   (live with >= {MIN_JOBS} posting)")
    print(f"  empty  {empty}   (200 but no postings today)")
    print(f"  dead   {dead}")
    print(f"  merged watchlist: {len(merged)}")

    per_company_sec = 784 / 385
    per_company_usd = 0.023 / 385
    print(f"\nprojected: {len(merged) * per_company_sec / 3600:.1f} compute-hours per full sweep")
    for shards in (8, 12, 16):
        per_shard = len(merged) / shards
        print(
            f"  shardCount {shards:>2}: {per_shard:>6.0f} boards/shard, "
            f"{per_shard * per_company_sec:>6.0f}s/run, "
            f"${per_shard * per_company_usd:.3f}/run, "
            f"${len(merged) * per_company_usd * 30.44:.2f}/month"
        )

    if not args.apply:
        print("\n(dry run — pass --apply to write)")
        return 0

    token = subprocess.run(
        ["apify", "auth", "token"], capture_output=True, text=True
    ).stdout.strip()
    req = urllib.request.Request(
        f"https://api.apify.com/v2/key-value-stores/{STORE_ID}/records/watchlist",
        data=json.dumps(merged, ensure_ascii=False).encode(),
        method="PUT",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=120)
    print(f"\nwrote watchlist: {len(merged)} boards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
