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

#: Dataset items over plain REST — no token, so no permission grant to get wrong.
ITEMS_URL = "https://api.apify.com/v2/datasets"

#: Column names upstream Actors use for the posting URL, in the order we try them. The
#: user can override with `urlField`; this is what makes the integration work without them
#: having to look.
DEFAULT_URL_FIELDS = ("jobUrl", "url", "applyUrl", "absolute_url", "hostedUrl", "link")

#: Rows per dataset page. Small enough that a huge upstream dataset never lands in memory
#: at once, large enough that a few thousand rows is a handful of round trips.
PAGE = 500

#: Tries per dataset page before a run gives up. A page that will not load must not look
#: like the end of the dataset.
REST_RETRIES = 3

#: How stale the published index may be before `stillListed` stops being a current fact.
#: The publisher runs daily; two days means it has missed twice.
MAX_INDEX_AGE_DAYS = 2

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


def _index_age_days(generated: str | None) -> int | None:
    """Days since the index was built, or None if it does not say."""
    if not isinstance(generated, str):
        return None
    try:
        built = datetime.strptime(generated[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (datetime.now(UTC).date() - built.date()).days


async def _make_reader(dataset_id: str):
    """A `(offset, limit) -> list[dict]` over the upstream dataset.

    Public REST first: it needs no token and therefore no permission grant, which is the
    whole difficulty. If that is refused the dataset is private, and only then is the
    scoped SDK client worth trying — it will work when the caller genuinely granted access
    and fail loudly when they did not, which is the honest outcome either way.
    """

    def _rest(offset: int, limit: int) -> list[dict[str, Any]] | None:
        url = f"{ITEMS_URL}/{dataset_id}/items?offset={offset}&limit={limit}&format=json"
        request = urllib.request.Request(url, headers={"User-Agent": "ats-jobs-lifecycle"})
        try:
            body = urllib.request.urlopen(request, timeout=60).read()
        except Exception:
            return None
        rows = json.loads(body)
        return rows if isinstance(rows, list) else []

    probe = await asyncio.to_thread(_rest, 0, 1)
    if probe is not None:
        logger.info("reading upstream dataset over public REST")

        async def read_rest(offset: int, limit: int) -> list[dict[str, Any]]:
            # A failed page and an exhausted dataset both used to arrive here as `[]`, and
            # the caller stops on `[]`. A blip in the middle of a 40,000-row dataset would
            # have ended the run early, reported success, and charged for the prefix. One
            # retry, then say so.
            for attempt in range(REST_RETRIES):
                rows = await asyncio.to_thread(_rest, offset, limit)
                if rows is not None:
                    return rows
                logger.warning("dataset page at offset %d failed (try %d)", offset, attempt + 1)
                await asyncio.sleep(2**attempt)
            raise RuntimeError(
                f"upstream dataset page at offset {offset} could not be read after "
                f"{REST_RETRIES} tries; refusing to report a truncated read as complete"
            )

        return read_rest

    logger.info("public REST refused; falling back to the scoped SDK client")
    dataset = await Actor.open_dataset(id=dataset_id)

    async def read_sdk(offset: int, limit: int) -> list[dict[str, Any]]:
        return (await dataset.get_data(offset=offset, limit=limit)).items

    return read_sdk


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

        # `stillListed: true` is a claim about *today*. It is only as current as the file
        # it came from, and nothing downstream can tell a fresh index from a month-old one.
        # If our publisher has stopped, fail rather than sell yesterday as now.
        generated = payload.get("generated")
        age = _index_age_days(generated)
        if age is None or age > MAX_INDEX_AGE_DAYS:
            await Actor.fail(
                status_message=(
                    f"The age index is {generated!r} ({age} days old); this Actor will not "
                    f"report listings as current from an index older than "
                    f"{MAX_INDEX_AGE_DAYS} days."
                )
            )
            return

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
        # Two ways to read the upstream dataset, and the SDK one is not the reliable half.
        # `Actor.open_dataset` uses the run's scoped token, and a limited-permission Actor
        # is refused with ForbiddenError even for a dataset in the same account — measured
        # 2026-09-08 against our own prior run. The REST items endpoint answers 200 for a
        # dataset whose access allows it, with no token at all, so try that first and keep
        # the SDK as the path for a private dataset the caller has actually granted.
        reader = await _make_reader(cfg["datasetId"])

        seen: set[str] = set()
        read = pushed = charged = 0
        by_coverage: dict[str, int] = {}
        offset = 0

        while read < cfg["maxRows"]:
            items = await reader(offset, min(PAGE, cfg["maxRows"] - read))
            if not items:
                break
            offset += len(items)

            for item in items:
                read += 1
                row = enricher.enrich(first_url(item, cfg["urlFields"]))
                row["indexGeneratedAt"] = generated
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
