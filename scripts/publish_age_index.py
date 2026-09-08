#!/usr/bin/env python3
"""Publish the public age index: job id -> posted date, for the boards we watch.

An Apify Actor runs under **the caller's** account, so it cannot read our key-value store.
That closed the integration path once already. Serving the same facts over plain HTTP
removes the wall entirely: the Actor fetches a file, and no permission model is involved.

**What goes out.** Live postings on lever, ashby and rippling: their id, the board's own
posted date, the day we first saw them, and when we started watching that board. Those
three are here because the board API's own job key appears in the public posting URL, so a
caller can join on it.

**What does not.** No titles, no locations, no descriptions, no salary, no company names
beyond the board slug that is already in every public URL — and above all no removals. The
paid product is the event stream: what appeared and what was removed, by day. This file is
the age of things that are up right now, which is what an enrichment caller needs and is
not what a subscriber pays for.

Personio is excluded here as it is from the free aggregates: its marketplace terms forbid
making the data publicly available. Greenhouse and Recruitee are absent for a different
reason — their public URLs cannot identify one posting, so an index of their ids would have
nothing to join against.

Usage::

    APIFY_TOKEN="$(apify auth token)" python scripts/publish_age_index.py \\
        --out ../hiring-closures/docs
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import pathlib
import sys
import urllib.request
from datetime import UTC, datetime

STORE_ID = "F97QIaKY3NR1xJ58y"
API = "https://api.apify.com/v2/key-value-stores"

#: Only providers whose public posting URL carries the board API's own job key. Publishing
#: ids nobody can join against would be noise with a licence attached.
PUBLISHED = ("lever", "ashby", "rippling")

SCHEMA = 1


def fetch(key: str, token: str) -> object:
    request = urllib.request.Request(
        f"{API}/{STORE_ID}/records/{key}", headers={"Authorization": f"Bearer {token}"}
    )
    blob = urllib.request.urlopen(request, timeout=120).read()
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
    return json.loads(blob)


def build(token: str, blocked: set[tuple[str, str]]) -> dict:
    """One entry per board, jobs nested, so the slug and tracked-since are not repeated.

    Measured 2026-09-08: 138,813 jobs over 6,315 boards is 9.2 MB of JSON and 3.3 MB
    gzipped in this shape, against 13.4 MB flat. The saving is what keeps a per-run fetch
    honest for the caller.
    """
    boards = []
    jobs = 0
    for bucket in range(64):
        try:
            record = fetch(f"state.{bucket:02d}", token)
        except Exception as error:  # a bucket that will not load must not publish silence
            print(f"  ! state.{bucket:02d}: {error}", file=sys.stderr)
            raise
        for key, entry in (record.get("companies") or {}).items():
            provider, _, company = key.partition(":")
            if provider not in PUBLISHED or (provider, company) in blocked:
                continue
            rows = [
                [job_id, job.get("posted"), job.get("first_seen")]
                for job_id, job in (entry.get("jobs") or {}).items()
            ]
            if not rows:
                continue
            jobs += len(rows)
            boards.append(
                {
                    "p": provider,
                    "c": company,
                    "t": entry.get("tracked_since"),
                    "j": sorted(rows, key=lambda row: row[0]),
                }
            )
    boards.sort(key=lambda board: (board["p"], board["c"].casefold()))
    return {
        "schema": SCHEMA,
        "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "providers": list(PUBLISHED),
        "boards": boards,
        "counts": {"boards": len(boards), "jobs": jobs},
        "note": (
            "Live postings only. Each row is [job_id, posted, first_seen]; `t` is the day "
            "we began watching that board, so `first_seen` equal to it is a floor on the "
            "posting's age rather than its age. Removals are not in this file."
        ),
    }


def load_blocklist(path: pathlib.Path) -> set[tuple[str, str]]:
    """`provider:slug` per line. An employer's exclusion applies here before anything else."""
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" in line:
            provider, _, slug = line.partition(":")
            out.add((provider.strip(), slug.strip()))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="directory to write age-index.json.gz into")
    parser.add_argument("--blocklist", default="../hiring-closures/blocklist.txt")
    args = parser.parse_args()

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print('set APIFY_TOKEN, e.g. APIFY_TOKEN="$(apify auth token)"', file=sys.stderr)
        return 1

    blocked = load_blocklist(pathlib.Path(args.blocklist))
    index = build(token, blocked)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(index, separators=(",", ":")).encode()
    packed = gzip.compress(raw, 9)
    (out / "age-index.json.gz").write_bytes(packed)

    # A tiny sidecar so a caller can see the size and date before pulling megabytes.
    (out / "age-index.meta.json").write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "generated": index["generated"],
                "counts": index["counts"],
                "bytes": len(packed),
                "url": "https://moonie0201.github.io/hiring-closures/age-index.json.gz",
            },
            indent=2,
        )
        + "\n"
    )
    print(
        f"age-index: {index['counts']['jobs']:,} jobs over "
        f"{index['counts']['boards']:,} boards, {len(packed) / 1024 / 1024:.2f} MB gzipped"
        + (f", {len(blocked)} blocked" if blocked else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
