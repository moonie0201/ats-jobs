#!/usr/bin/env python3
"""Package the paid archive into the one file a buyer downloads.

Input: the per-day archive `scripts/export_closures.py` writes (`{src}/archive/{day}/`).
Output: `{out}/observations-{cutoff}.csv.gz`, the same rows as `.jsonl.gz`, `schema.md`
with the coverage table, and `sample.csv` (five real rows for the product page).

Packaging only — no new fields, no new inference. Two rules enforced here because the
sold file is "making available" in the Directive 96/9/EC sense and the compliance line
depends on them:

* **Personio rows are dropped.** `spec/COMPLIANCE.md` keeps that provider out of every
  public or sold artefact under its marketplace terms (§4.2).
* **Nothing is added that the archive does not contain.** The coverage table is computed
  from the rows and from `tracked_since` in the store's state, so a buyer can see how thin
  the observation window is before paying.

    python scripts/bundle_export.py --src ../closures-private --out ../closures-private/bundle
"""

from __future__ import annotations

import argparse
import collections
import csv
import gzip
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from core.diff import EVENT_KEYS  # noqa: E402

EXCLUDED_PROVIDERS = {"personio"}
STORE_ID = "F97QIaKY3NR1xJ58y"


def tracked_since(token: str) -> dict[str, str]:
    """`provider:company -> tracked_since` from the 64 state buckets."""
    out: dict[str, str] = {}
    for b in range(64):
        req = urllib.request.Request(
            f"https://api.apify.com/v2/key-value-stores/{STORE_ID}/records/state.{b:02d}",
            headers={"Authorization": f"Bearer {token}"},
        )
        blob = urllib.request.urlopen(req, timeout=120).read()
        if blob[:2] == b"\x1f\x8b":
            blob = gzip.decompress(blob)
        for key, entry in (json.loads(blob).get("companies") or {}).items():
            if entry.get("tracked_since"):
                out[key] = entry["tracked_since"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print('set APIFY_TOKEN="$(apify auth token)"', file=sys.stderr)
        return 1

    days = sorted(p.name for p in (args.src / "archive").iterdir() if p.is_dir())
    cutoff = days[-1]
    rows: list[dict] = []
    dropped = 0
    for day in days:
        with gzip.open(args.src / "archive" / day / "events.jsonl.gz", "rt") as fh:
            for line in fh:
                r = json.loads(line)
                if r["provider"] in EXCLUDED_PROVIDERS:
                    dropped += 1
                    continue
                rows.append(r)

    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.out / f"observations-{cutoff}"
    with gzip.open(f"{stem}.csv.gz", "wt", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(EVENT_KEYS), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with gzip.open(f"{stem}.jsonl.gz", "wt") as fh:
        for r in rows:
            fh.write(json.dumps({k: r.get(k) for k in EVENT_KEYS}, ensure_ascii=False) + "\n")

    # Coverage: what a buyer must see before paying.
    since = tracked_since(token)
    by_prov = collections.Counter(r["provider"] for r in rows)
    by_ev = collections.Counter(r["ev"] for r in rows)
    boards = {f"{r['provider']}:{r['company']}" for r in rows}
    since_hist = collections.Counter(since.get(b, "unknown") for b in boards)
    per_day = collections.Counter(r["d"] for r in rows)
    verified = collections.Counter(str(r.get("verified")) for r in rows if r["ev"] == "removed")

    lines = [
        f"# observations-{cutoff} — schema and coverage",
        "",
        f"Rows: **{len(rows):,}** events over **{len(boards):,}** boards, "
        f"{days[0]} → {cutoff} (UTC). Personio rows excluded: {dropped:,}.",
        "",
        "## Coverage",
        "",
        "| provider | events |",
        "|---|---|",
        *[f"| {p} | {n:,} |" for p, n in by_prov.most_common()],
        "",
        "| event | rows |",
        "|---|---|",
        *[f"| {e} | {n:,} |" for e, n in by_ev.most_common()],
        "",
        "Boards by the day observation began (`tracked_since`). A posting already open on "
        "that day carries a first-seen date that is a floor, not an age:",
        "",
        "| tracked_since | boards |",
        "|---|---|",
        *[f"| {d} | {n:,} |" for d, n in sorted(since_hist.items())],
        "",
        "Events per observation day. A day with few rows is a day on which few boards were "
        "measured, not a quiet day; absent company-days are absent, never zero:",
        "",
        "| day | events |",
        "|---|---|",
        *[f"| {d} | {n:,} |" for d, n in sorted(per_day.items())],
        "",
        "`verified` on `removed` rows (Greenhouse/Lever single-posting endpoint also 404):",
        "",
        "| verified | rows |",
        "|---|---|",
        *[f"| {v} | {n:,} |" for v, n in verified.most_common()],
        "",
        "## Schema",
        "",
        "| field | meaning |",
        "|---|---|",
        "| `d` | observation day, UTC |",
        "| `provider` | greenhouse · lever · ashby · recruitee · rippling |",
        "| `company` | board slug as in the public URL |",
        "| `job_id` | the board API's own id |",
        "| `ev` | `added` (first seen) · `removed` (no longer served) · `changed` |",
        "| `t`, `loc`, `dept` | title, location, department as published |",
        "| `url` | public posting URL |",
        "| `posted` | the board's own posted date, where published |",
        "| `verified` | removed rows only: true / false / null (see above) |",
        "",
        "## What this is not",
        "",
        "Not a claim that a role was filled, cancelled or fake. Not complete history: "
        "observation starts at `tracked_since`. Not a feed: a dated snapshot to the cutoff. "
        "No personal data.",
    ]
    (args.out / "schema.md").write_text("\n".join(lines) + "\n")

    with open(args.out / "sample.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(EVENT_KEYS), extrasaction="ignore")
        w.writeheader()
        picks = [r for r in rows if r["ev"] == "removed" and r.get("verified")][:3]
        picks += [r for r in rows if r["ev"] == "added"][:2]
        w.writerows(picks)

    print(f"bundle: {len(rows):,} rows, {len(boards):,} boards, cutoff {cutoff}, "
          f"{dropped:,} personio rows dropped -> {args.out}")
    for p in sorted(args.out.iterdir()):
        print(f"  {p.name:<36} {p.stat().st_size/1024/1024:6.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
