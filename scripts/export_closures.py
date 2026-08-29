#!/usr/bin/env python3
"""Export the `ats-history` closure record: a free company-day sample, the paid archive.

    APIFY_TOKEN="$(apify auth token)" python scripts/export_closures.py --sample
    APIFY_TOKEN="$(apify auth token)" python scripts/export_closures.py --since 2026-08-26

Three artefacts out of one pass over the store:

* ``--sample`` -> ``{out}/sample/closures-72h.{csv,jsonl}``. The FREE tier, and the only
  thing that may ever be published. Exactly :data:`FREE_FIELDS`: six columns, one row per
  company per day, the three most recent days. No job-level rows, no free text, no URLs,
  no ids -- the legal ruling's publishable set and nothing else, enforced by projection
  here and by ``tests/test_export_closures.py`` on the way out.
* default -> ``{out}/archive/{day}/events.{jsonl,csv}.gz``. The PAID tier: the thirteen
  :data:`~core.diff.EVENT_KEYS`, partitioned by day, gzipped. Written to a local
  directory that is **not** a publishable repo, because publishing job-level rows is
  "making available to the public" under Directive 96/9/EC Art. 7(2)(b) and the whole
  free/paid line rests on not doing it.

  ``verified`` (on ``removed`` rows only, else null) says which signals stand behind the
  closure. ``true``: the feed dropped the job *and* the provider's single-posting endpoint
  answered 404 -- Greenhouse and Lever, from 2026-08-29. ``false``: the feed dropped it
  but that endpoint could not be asked (5xx/429/timeout, or the run's verification cap)
  on three consecutive sweeps, so the row is up to three days late. ``null``: Ashby,
  Recruitee, Rippling and Personio have no such endpoint, so feed membership is the only
  signal; and every row before 2026-08-29 for any provider. A job the feed dropped while
  the endpoint still served it is **not** a row at all -- it stays open in state.
* default -> ``{out}/summary/closures-daily.{csv,jsonl}``. Full-depth company-day
  aggregates plus ``net``; the sample is this table, windowed and with ``net`` dropped.
  Only a run with no ``--since``/``--until`` writes that name -- a windowed run writes
  ``closures-daily.{first}_{last}`` instead, so a narrow re-run cannot leave one day in the
  file the "full depth" tier is delivered from.

**Why a company-day can be missing, and why that is not a zero.** `counts.{day}.{shard}`
gets a row only on the branch of `actors/ats-history-snapshot/src/main.py` that actually
diffed a board (`run.counts.append`, right after `diff()`). A failed fetch, a `stale`
company, a degraded provider and an `EMPTY_SUSPECT` day all `continue` before that line,
writing no count and no events. So **a counts row is the "we measured you" marker**, and
this exporter emits a company-day only when one exists. Absent days are absent, never 0.

**Why `open == 0 and removed > 0` is dropped.** That shape has exactly one code path: the
board returned `[]` while state held jobs, which sets `EMPTY_SUSPECT` and emits nothing;
only a *second* consecutive empty response lets `diff()` run and remove the whole board at
once. A confirmed-empty board and a two-day outage or a board migration are the same bytes,
so the wipe is suppressed rather than sold as a closure. Disclosure D3's promise -- "the
data therefore under-reports closures rather than inventing them" -- is this function.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
for _p in (str(REPO), str(REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from build_directory import (  # noqa: E402  (needs the sys.path above)
    BLOCKLIST_PATH,
    PUBLISH_DENY_PROVIDERS,
    gzip_bytes,
    read_blocklist,
    to_jsonl,
)

from core.diff import EVENT_KEYS  # noqa: E402
from core.history import STORE_NAME, decode_text, from_jsonl  # noqa: E402

API = "https://api.apify.com/v2"

#: The free tier, verbatim from the legal ruling. Our own clock, our own enum, a slug
#: already CC0 in `moonie0201/ats-directory`, and three integers we computed ourselves.
#: Nothing here is copied from a provider or an employer, which is the whole argument.
FREE_FIELDS: tuple[str, ...] = ("d", "provider", "company", "open", "added", "removed")

#: Paid only: `net` is arithmetic on two free columns, but the ruling fixed the free tier
#: at six columns and "six" is easier to audit than "six plus anything derivable".
SUMMARY_FIELDS: tuple[str, ...] = (*FREE_FIELDS, "net")

#: Never emitted anywhere, at any tier, by any code path. Kept as data so the test can
#: assert on the same list the exporter is written against.
FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "description",
        "descriptionHtml",
        "descriptionText",
        "body",
        "content",
        "summary",
        "excerpt",
        "snippet",
        "salary",
        "compensation",
        "salaryMin",
        "salaryMax",
        "pay",
        "raw",
        "rawJson",
        "recruiter",
        "hiringManager",
        "contact",
        "email",
        "mailbox_email",
        "phone",
        "options_phone",
        "name",
        "candidate",
    }
)

#: "the most recent 72 hours" on a store whose grain is a day.
SAMPLE_DAYS = 3


# ------------------------------------------------------------------------ store IO


def api_token() -> str:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        sys.exit('set APIFY_TOKEN, e.g. APIFY_TOKEN="$(apify auth token)"')
    return token


def get(path: str, token: str) -> bytes:
    req = urllib.request.Request(f"{API}{path}")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=90) as response:
        return response.read()


def list_keys(store: str, token: str) -> list[str]:
    """Every key in the store. Paginated: 400 days x 4 shards x 2 kinds outgrows one page."""
    keys: list[str] = []
    cursor = ""
    while True:
        page = json.loads(get(f"/key-value-stores/{store}/keys?limit=1000{cursor}", token))["data"]
        keys.extend(item["key"] for item in page["items"])
        if not page.get("isTruncated"):
            return keys
        cursor = f"&exclusiveStartKey={urllib.parse.quote(keys[-1])}"


def day_keys(keys: list[str], kind: str, since: str | None, until: str | None) -> list[str]:
    """`{kind}.{YYYY-MM-DD}.{shard}` within the window. Days come from the store, not from
    a generated date range: a day nobody collected has no key and must stay absent."""
    out = []
    for key in keys:
        head, _, rest = key.partition(".")
        day = rest.partition(".")[0]
        if head == kind and len(day) == 10 and not (since and day < since or until and day > until):
            out.append(key)
    return sorted(out)


def read_jsonl(store: str, token: str, keys: list[str]) -> list[dict[str, Any]]:
    """Values are gzipped JSONL (`core.history.encode_text`). IO-bound, so threads."""
    if not keys:
        return []
    with ThreadPoolExecutor(max_workers=8) as pool:
        blobs = pool.map(lambda k: get(f"/key-value-stores/{store}/records/{k}", token), keys)
        return [row for blob in blobs for row in from_jsonl(decode_text(blob))]


# ---------------------------------------------------------------------- aggregation


def _ck(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("d")), str(row.get("provider")), str(row.get("company")))


def first_days(counts: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """(provider, company) -> earliest measured day. Every counts row is ~4 KB gzipped per
    shard-day, so reading all of them to find each board's baseline is cheap."""
    out: dict[tuple[str, str], str] = {}
    for c in counts:
        key = (str(c.get("provider")), str(c.get("company")))
        day = str(c.get("d"))
        if key not in out or day < out[key]:
            out[key] = day
    return out


def summarise(
    counts: list[dict[str, Any]],
    events: list[dict[str, Any]],
    first_day: dict[tuple[str, str], str] | None = None,
) -> tuple[list[dict[str, Any]], set[tuple[str, str, str]]]:
    """counts + events -> one row per *measured* company-day, and the suppressed keys.

    Returns ``(rows, suppressed)``. A key is suppressed when the day is the confirmed-empty
    end of an `EMPTY_SUSPECT` chain (see the module docstring); a company-day with no counts
    row is not measured and simply never appears.
    """
    tally: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for event in events:
        key = _ck(event)
        if event.get("ev") == "added":
            tally[key][0] += 1
        elif event.get("ev") == "removed":
            tally[key][1] += 1

    # dict, not list: a re-run with `force` rewrites its shard's counts, and the last
    # measurement of a company-day is the one that matches the events we just read.
    measured = {_ck(c): c for c in counts}

    # A board's first measured day marks every posting `added` (there is no earlier
    # snapshot to diff against). That is a first sighting, not hiring — publishing it as
    # `added` misled the 72 h sample on 2026-08-26/27. Emit null instead, so a reader
    # summing `added` never counts a baseline as growth.
    # `first_day` must be computed over the WHOLE store, not the window being exported:
    # a 3-day sample window flagged 08-27 as the baseline for the 385 boards first seen on
    # 08-26 and blanked their real `added`. Callers pass :func:`first_days`; the fallback
    # below is only right when `counts` is the full store.
    if first_day is None:
        first_day = first_days(counts)

    rows: list[dict[str, Any]] = []
    suppressed: set[tuple[str, str, str]] = set()
    for key, count in sorted(measured.items()):
        added, removed = tally.get(key, [0, 0])
        open_now = int(count.get("open") or 0)
        if open_now == 0 and removed:
            suppressed.add(key)
            continue
        day, provider, company = key
        baseline = first_day[(provider, company)] == day
        rows.append(
            {
                "d": day,
                "provider": provider,
                "company": company,
                "open": open_now,
                "added": None if baseline else added,
                "removed": removed,
                "net": None if baseline else added - removed,
            }
        )
    return rows, suppressed


def trustworthy_events(
    events: list[dict[str, Any]],
    counts: list[dict[str, Any]],
    suppressed: set[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """Archive events, projected onto :data:`~core.diff.EVENT_KEYS` and nothing else.

    Same two gates as the summary, for the same reason: a buyer must not receive the
    closures the free file is honest enough to withhold.
    """
    ok = {_ck(c) for c in counts} - suppressed
    return sorted(
        ({k: e.get(k) for k in EVENT_KEYS} for e in events if _ck(e) in ok),
        key=lambda e: (e["d"], e["provider"], e["company"], e["ev"], str(e["job_id"])),
    )


def publishable(rows: list[dict[str, Any]], blocked: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """Free-tier rows: Personio out (Marketplace ToS §4.2 forbids a *publicly available*
    directory), blocklist out, projected to exactly six columns."""
    return [
        {k: r[k] for k in FREE_FIELDS}
        for r in rows
        if r["provider"] not in PUBLISH_DENY_PROVIDERS
        and (r["provider"], r["company"]) not in blocked
    ]


def unblocked(rows: list[dict[str, Any]], blocked: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """Paid-tier rows. Personio stays (a contracted delivery is not a public directory);
    an employer's own exclusion request is honoured at every tier."""
    return [r for r in rows if (r["provider"], r["company"]) not in blocked]


def recent_days(rows: list[dict[str, Any]], window: int = SAMPLE_DAYS) -> list[dict[str, Any]]:
    days = sorted({r["d"] for r in rows})[-window:]
    return [r for r in rows if r["d"] in days]


# -------------------------------------------------------------------------- output


def to_csv(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> bytes:
    """`changed` is a list; everything else renders itself. `None` -> empty cell, which is
    what "we did not measure this" has to look like in a CSV."""
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=list(fields), lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    for row in rows:
        out = {k: row.get(k) for k in fields}
        if isinstance(out.get("changed"), list):
            out["changed"] = "|".join(out["changed"])
        writer.writerow({k: "" if v is None else v for k, v in out.items()})
    return buf.getvalue().encode("utf-8")


def write(path: Path, blob: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


def write_pair(
    stem: Path, rows: list[dict[str, Any]], fields: tuple[str, ...], *, gz: bool
) -> None:
    csv_blob, jsonl_blob = to_csv(rows, fields), to_jsonl(rows, fields)
    suffix = ".gz" if gz else ""
    write(stem.with_name(stem.name + ".csv" + suffix), gzip_bytes(csv_blob) if gz else csv_blob)
    write(
        stem.with_name(stem.name + ".jsonl" + suffix),
        gzip_bytes(jsonl_blob) if gz else jsonl_blob,
    )


def export(
    rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    out: Path,
    *,
    sample: bool,
    blocked: set[tuple[str, str]],
    windowed: bool = False,
) -> dict[str, int]:
    """Write one tier. Returns what went out, for the run summary."""
    if sample:
        free = recent_days(publishable(rows, blocked))
        write_pair(out / "sample" / "closures-72h", free, FREE_FIELDS, gz=False)
        return {"rows": len(free), "days": len({r["d"] for r in free}), "events": 0}

    paid = unblocked(rows, blocked)
    # `closures-daily` is what the "full, from <first day>" tier is delivered from, so only a
    # full-depth run may write it. A `--since`/`--until` run names itself for its span instead;
    # otherwise a narrow re-run silently leaves one day in the file the buyer is promised all
    # of. The archive is already day-partitioned and needs no equivalent.
    days = sorted({r["d"] for r in paid})
    stem = "closures-daily"
    if windowed and days:
        stem = f"closures-daily.{days[0]}_{days[-1]}"
    write_pair(out / "summary" / stem, paid, SUMMARY_FIELDS, gz=False)
    kept = [e for e in events if (e["provider"], e["company"]) not in blocked]
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in kept:
        by_day[event["d"]].append(event)
    for day, day_events in by_day.items():
        write_pair(out / "archive" / day / "events", day_events, EVENT_KEYS, gz=True)
    return {"rows": len(paid), "days": len(by_day), "events": len(kept)}


# ----------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", help="first observation day, YYYY-MM-DD (default: earliest stored)")
    ap.add_argument("--until", help="last observation day, YYYY-MM-DD (default: latest stored)")
    ap.add_argument(
        "--sample",
        action="store_true",
        help=f"free tier: {SAMPLE_DAYS} days, {len(FREE_FIELDS)} aggregate columns, publishable",
    )
    ap.add_argument("--out", type=Path, help="output directory (default depends on --sample)")
    ap.add_argument("--store", default=f"acotr_moonie~{STORE_NAME}", help="store id or user~name")
    args = ap.parse_args(argv)

    # The archive default is *outside* every git repo on purpose: job-level rows one
    # `git add -A` from a public remote is the failure the free/paid line exists to prevent.
    default = "hiring-closures/data" if args.sample else "closures-archive"
    out = args.out or REPO.parent / default
    token = api_token()
    try:
        keys = list_keys(args.store, token)
        since = args.since
        if args.sample and not since:
            # The sample is the last SAMPLE_DAYS days *present in the store*. Without this the
            # nightly run downloads all 400 retained days to publish three, and races
            # `HistoryStore.prune`: a key listed a moment ago can be deleted before we GET it,
            # and the 404 aborts the run.
            stored = sorted({k.split(".")[1] for k in keys if k.startswith("counts.")})
            since = stored[-SAMPLE_DAYS:][0] if stored else None
        counts = read_jsonl(args.store, token, day_keys(keys, "counts", since, args.until))
        events = read_jsonl(args.store, token, day_keys(keys, "events", since, args.until))
        # Baselines come from every counts day in the store, never from the window.
        all_counts = (
            counts
            if since is None and args.until is None
            else read_jsonl(args.store, token, day_keys(keys, "counts", None, None))
        )
    except urllib.error.HTTPError as exc:
        sys.exit(f"Apify API {exc.code}: {exc.reason} — check APIFY_TOKEN and --store")

    rows, suppressed = summarise(counts, events, first_days(all_counts))
    # `export(sample=True)` never reads `events`, and projecting + sorting a copy of every
    # event in the store is the second-largest cost of a nightly sample run.
    archive = [] if args.sample else trustworthy_events(events, counts, suppressed)
    blocked = read_blocklist(BLOCKLIST_PATH)
    written = export(
        rows,
        archive,
        out,
        sample=args.sample,
        blocked=blocked,
        windowed=bool(args.since or args.until),
    )

    days = sorted({r["d"] for r in rows})
    span = f"{len(days)} ({days[0]} .. {days[-1]})" if days else "0"
    print(
        f"{'sample' if args.sample else 'archive'} -> {out}\n"
        f"  days read       {span}\n"
        f"  measured        {len(rows)} company-days over {len({(r['provider'], r['company']) for r in rows})} companies\n"  # noqa: E501
        f"  suppressed      {len(suppressed)} company-days (empty-board chain, not a closure)\n"
        f"  events read     {len(events)}  kept {'n/a (sample)' if args.sample else len(archive)}\n"  # noqa: E501
        f"  opened/closed   {sum(r['added'] or 0 for r in rows)} / "
        f"{sum(r['removed'] for r in rows)}"
        f"  net {sum(r['net'] or 0 for r in rows)}\n"
        f"  written         {written['rows']} rows, {written['events']} events, "
        f"{written['days']} day partitions"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
