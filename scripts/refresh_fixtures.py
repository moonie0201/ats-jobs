#!/usr/bin/env python3
"""Re-download the unit-test fixtures from the live public endpoints (SPEC v2 §10.1).

Every fixture under ``tests/fixtures/<provider>/`` is a real payload captured from the
vendor's own public job-board API. This script fetches them again, writes them back and
reports which ones changed, so ``contract.yml`` can open a PR when a provider's payload
*shape* moves (§10.2).

    python scripts/refresh_fixtures.py                  # everything, full payloads
    python scripts/refresh_fixtures.py --provider ashby --limit 25
    python scripts/refresh_fixtures.py --limit 25       # trim big boards before committing

``--limit`` keeps the first N postings of a list payload. The shape is untouched — it is
there because the Ashby openai board alone is 12 MB and a repo does not need all of it.

Exit code 1 means at least one endpoint answered with something other than the status the
fixture expects: either the slug went dark or the provider changed. That is the tripwire.

Fixtures that are *not* managed here, because no live endpoint produces them: the
hand-built ``_empty_objects/`` set (§10.1), ``lever/eu_only.json``,
``lever/global_empty.json``, ``personio/malformed.xml`` and the Recruitee 301
``careers_not_hosted`` page. Those are committed once and edited by hand.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

USER_AGENT = "ats-jobs-scraper/0.1 (+https://github.com/ats-jobs/ats-jobs)"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
TIMEOUT = 60.0
#: Politeness between requests; every provider's cap is 2 rps or unknown (§5.12).
PAUSE_SECS = 0.5

RIPPLING_BOARD = "rippling"
RIPPLING_LIST = f"https://api.rippling.com/platform/api/ats/v1/board/{RIPPLING_BOARD}/jobs"
#: How many Rippling jobs to probe when hunting for a nested department tree.
RIPPLING_DETAIL_PROBES = 12


@dataclass(frozen=True)
class Fixture:
    provider: str
    name: str
    url: str
    expect: int = 200

    @property
    def path(self) -> Path:
        return FIXTURES / self.provider / self.name


#: The sample boards of §10. Each is a real, non-empty board except the deliberate
#: not-found captures, which lock down the §5.12 failure path.
CATALOG: tuple[Fixture, ...] = (
    Fixture(
        "greenhouse",
        "anthropic.json",
        "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs"
        "?content=true&pay_transparency=true",
    ),
    Fixture(
        "greenhouse",
        "stripe_customdomain.json",
        "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true&pay_transparency=true",
    ),
    Fixture(
        "greenhouse",
        "airbnb_paytransparency.json",
        "https://boards-api.greenhouse.io/v1/boards/airbnb/jobs?content=true&pay_transparency=true",
    ),
    Fixture(
        "greenhouse",
        "404.json",
        "https://boards-api.greenhouse.io/v1/boards/acme-that-does-not-exist/jobs?content=true",
        expect=404,
    ),
    Fixture("lever", "palantir.json", "https://api.lever.co/v0/postings/palantir?mode=json"),
    Fixture(
        "ashby",
        "openai.json",
        "https://api.ashbyhq.com/posting-api/job-board/openai?includeCompensation=true",
    ),
    Fixture("recruitee", "channable.json", "https://channable.recruitee.com/api/offers/"),
    Fixture("recruitee", "nmbrs.json", "https://nmbrs.recruitee.com/api/offers/"),
    Fixture(
        "recruitee",
        "not_found.json",
        "https://acme-that-does-not-exist.recruitee.com/api/offers/",
        expect=404,
    ),
    Fixture("rippling", "list_dupes.json", RIPPLING_LIST),
    Fixture("personio", "sample.xml", "https://personio.jobs.personio.de/xml?language=en"),
)


def fetch(client: httpx.Client, url: str) -> httpx.Response:
    time.sleep(PAUSE_SECS)
    return client.get(url)


def trim(payload: Any, limit: int) -> Any:
    """Keep the first ``limit`` postings, whatever the provider calls its list."""
    if not limit:
        return payload
    if isinstance(payload, list):
        return payload[:limit]
    if isinstance(payload, dict):
        for key in ("jobs", "offers", "data", "results", "postings", "positions"):
            value = payload.get(key)
            if isinstance(value, list):
                return {**payload, key: value[:limit]}
    return payload


def write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.read_text(encoding="utf-8") if path.exists() else None
    if before == text:
        return "unchanged"
    path.write_text(text, encoding="utf-8")
    return "created" if before is None else "updated"


def render(response: httpx.Response, name: str, limit: int) -> str:
    if not name.endswith(".json"):
        return response.text
    payload = trim(response.json(), limit)
    return json.dumps(payload, indent=1, ensure_ascii=False) + "\n"


def save(fixture: Fixture, response: httpx.Response, limit: int, problems: list[str]) -> None:
    if response.status_code != fixture.expect:
        problems.append(
            f"{fixture.provider}/{fixture.name}: expected HTTP {fixture.expect}, "
            f"got {response.status_code} from {fixture.url}"
        )
        return
    try:
        text = render(response, fixture.name, limit)
    except json.JSONDecodeError as exc:
        problems.append(f"{fixture.provider}/{fixture.name}: body is not JSON — {exc}")
        return
    state = write(fixture.path, text)
    print(f"  {state:9} {fixture.provider}/{fixture.name}  ({len(text):,} bytes)")


def refresh_rippling_details(client: httpx.Client, problems: list[str]) -> None:
    """Rippling's ad body, employment type, dates and pay live only in the detail call
    (§5.7), so two detail fixtures are derived from the list: the first job, and the first
    job whose ``department`` carries a multi-level ``department_tree``."""
    listing = fetch(client, RIPPLING_LIST)
    if listing.status_code != 200:
        problems.append(f"rippling: list returned {listing.status_code}, cannot derive details")
        return
    jobs = listing.json()
    uuids = [job.get("uuid") for job in jobs if isinstance(job, dict) and job.get("uuid")]
    seen: list[str] = []
    for uuid in uuids:
        if uuid not in seen:
            seen.append(uuid)
        if len(seen) >= RIPPLING_DETAIL_PROBES:
            break
    if not seen:
        problems.append("rippling: list payload carries no uuid")
        return

    first: tuple[str, dict[str, Any]] | None = None
    tree: tuple[str, dict[str, Any], list[str]] | None = None
    for uuid in seen:
        detail = fetch(client, f"{RIPPLING_LIST}/{uuid}")
        if detail.status_code != 200:
            continue
        body = detail.json()
        if first is None:
            first = (uuid, body)
        branches = (body.get("department") or {}).get("department_tree") or []
        # Prefer a *different* job for the hierarchy fixture, so the two files exercise
        # two payloads; fall back to the first job when it is the only one with a tree.
        if len(branches) > 1 and (tree is None or tree[0] == first[0]):
            tree = (uuid, body, branches)
        if first is not None and tree is not None and tree[0] != first[0]:
            break

    if first is None:
        problems.append("rippling: no job detail returned HTTP 200")
        return
    state = write(
        FIXTURES / "rippling" / "detail.json",
        json.dumps(first[1], indent=1, ensure_ascii=False) + "\n",
    )
    print(f"  {state:9} rippling/detail.json  (uuid {first[0]})")

    if tree is None:
        problems.append(
            f"rippling: no multi-level department_tree in the first {len(seen)} jobs — "
            "the §5.7 hierarchy claim needs re-checking"
        )
        return
    state = write(
        FIXTURES / "rippling" / "detail_dept_tree.json",
        json.dumps(tree[1], indent=1, ensure_ascii=False) + "\n",
    )
    print(f"  {state:9} rippling/detail_dept_tree.json  (uuid {tree[0]}, tree {tree[2]})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--provider",
        action="append",
        default=[],
        help="only this provider (repeatable); default: all six",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="keep only the first N postings of each list payload (0 = the whole board)",
    )
    args = parser.parse_args(argv)

    wanted = {p.strip().lower() for value in args.provider for p in value.split(",") if p.strip()}
    selected = [f for f in CATALOG if not wanted or f.provider in wanted]
    if not selected:
        print(f"no fixtures for {sorted(wanted)}", file=sys.stderr)
        return 1

    problems: list[str] = []
    print(f"refreshing {len(selected)} fixture(s) into {FIXTURES}")
    with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
        for fixture in selected:
            try:
                response = fetch(client, fixture.url)
            except httpx.HTTPError as exc:
                problems.append(f"{fixture.provider}/{fixture.name}: {type(exc).__name__} — {exc}")
                continue
            save(fixture, response, args.limit, problems)

        if not wanted or "rippling" in wanted:
            try:
                refresh_rippling_details(client, problems)
            except httpx.HTTPError as exc:
                problems.append(f"rippling detail: {type(exc).__name__} — {exc}")

    for problem in problems:
        print(f"PROBLEM {problem}", file=sys.stderr)
    if problems:
        print(
            f"{len(problems)} problem(s) — a slug went dark or a provider changed", file=sys.stderr
        )
        return 1
    print("fixtures OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
