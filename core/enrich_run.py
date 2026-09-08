"""Runner for `ats-jobs-lifecycle` — the Actor-to-Actor integration target (§3, §9.1).

Apify hands an integration target the *id* of the upstream dataset, never its contents, so
this fetches in pages rather than loading a dataset that may be far larger than memory.
The dynamic half of the input arrives in the implicit ``payload`` field the platform
attaches, which is why a user connecting this in the Integrations UI only has to fill in
the static half.

Charging: one ``lifecycle`` event per row that actually delivers an age. Rows we could not
resolve, providers we cannot identify, boards we do not watch and ids we have never seen
are all pushed — they are the answer — and none of them are charged.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import urllib.request
from datetime import UTC, datetime
from typing import Any

from apify import Actor

from core.lifecycle import INDEX_URL, Enricher, first_url, is_chargeable, load_index

logger = logging.getLogger("apify")

LIFECYCLE_EVENT = "lifecycle"

#: Column names upstream Actors use for the posting URL, in the order we try them. The
#: user can override with `urlField`; this is what makes the integration work without them
#: having to look.
DEFAULT_URL_FIELDS = ("jobUrl", "url", "applyUrl", "absolute_url", "hostedUrl", "link")

#: Rows per dataset page. Small enough that a huge upstream dataset never lands in memory
#: at once, large enough that a few thousand rows is a handful of round trips.
PAGE = 500

#: A run that would enrich more than this stops and says so. An integration fires on
#: somebody else's schedule, so the ceiling has to be ours.
DEFAULT_MAX_ROWS = 5_000


def _fetch_index() -> dict[str, Any]:
    """The published index, or a hard failure. A stale answer is worse than none."""
    request = urllib.request.Request(INDEX_URL, headers={"User-Agent": "ats-jobs-lifecycle"})
    blob = urllib.request.urlopen(request, timeout=120).read()
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
    return json.loads(blob)


def _config(raw: dict[str, Any]) -> dict[str, Any]:
    """Static input, plus the dataset id from wherever this run was started.

    Three ways in, all of them real: the platform's implicit `payload` when used as an
    integration, an explicit `datasetId` when the user prefills
    `{{resource.defaultDatasetId}}`, and a plain id when someone runs it by hand.
    """
    payload = raw.get("payload")
    resource = (payload or {}).get("resource") if isinstance(payload, dict) else None
    dataset_id = raw.get("datasetId") or (resource or {}).get("defaultDatasetId")

    fields = raw.get("urlField") or []
    if isinstance(fields, str):
        fields = [fields]
    fields = tuple(f for f in fields if isinstance(f, str) and f.strip())

    max_rows = raw.get("maxRows")
    try:
        max_rows = int(max_rows) if max_rows else DEFAULT_MAX_ROWS
    except (TypeError, ValueError):
        max_rows = DEFAULT_MAX_ROWS

    return {
        "datasetId": dataset_id if isinstance(dataset_id, str) and dataset_id else None,
        "urlFields": fields + DEFAULT_URL_FIELDS,
        "maxRows": max(1, min(max_rows, 100_000)),
        "keepSourceFields": bool(raw.get("keepSourceFields", False)),
    }


async def main() -> None:
    async with Actor:
        cfg = _config(await Actor.get_input() or {})
        if not cfg["datasetId"]:
            await Actor.fail(
                status_message=(
                    "No dataset to read. Connect this Actor as an integration on another "
                    "Actor, or set datasetId to {{resource.defaultDatasetId}}."
                )
            )
            return

        # Fetched over plain HTTP, not read from our key-value store. An Actor runs under
        # the caller's account and cannot reach our storage — that is what broke the first
        # version of this, deployed 2026-09-07. A public file has no permission model.
        payload = await asyncio.to_thread(_fetch_index)
        enricher = Enricher(
            boards=load_index(payload),
            today=datetime.now(UTC).date(),
            generated=payload.get("generated"),
        )
        logger.info(
            "age index %s: %s boards, %s jobs",
            payload.get("generated"),
            (payload.get("counts") or {}).get("boards"),
            (payload.get("counts") or {}).get("jobs"),
        )
        dataset = await Actor.open_dataset(id=cfg["datasetId"])

        seen: set[str] = set()
        read = pushed = charged = 0
        by_coverage: dict[str, int] = {}
        offset = 0

        while read < cfg["maxRows"]:
            page = await dataset.get_data(offset=offset, limit=min(PAGE, cfg["maxRows"] - read))
            items = page.items
            if not items:
                break
            offset += len(items)

            for item in items:
                read += 1
                row = enricher.enrich(first_url(item, cfg["urlFields"]))
                by_coverage[row["coverage"]] = by_coverage.get(row["coverage"], 0) + 1

                # A dataset may list the same posting under several rows. The later ones
                # are still returned — dropping a caller's row silently is worse than a
                # duplicate — but they are not charged twice for one lookup.
                key = f"{row['provider']}:{row['company']}:{row['jobId']}"
                duplicate = row["jobId"] is not None and key in seen
                if not duplicate and row["jobId"] is not None:
                    seen.add(key)
                row["duplicateOf"] = key if duplicate else None

                if cfg["keepSourceFields"]:
                    row = {**item, **row}

                if is_chargeable(row) and not duplicate:
                    await Actor.push_data(row, charged_event_name=LIFECYCLE_EVENT)
                    charged += 1
                else:
                    await Actor.push_data(row)
                pushed += 1

        logger.info(
            "lifecycle read=%d pushed=%d charged=%d coverage=%s",
            read,
            pushed,
            charged,
            by_coverage,
        )
        await Actor.set_status_message(
            f"{charged} of {read} rows enriched "
            f"({by_coverage.get('tracked', 0)} tracked, "
            f"{by_coverage.get('board_untracked', 0)} boards not watched, "
            f"{by_coverage.get('unsupported_provider', 0)} unsupported provider)"
        )
