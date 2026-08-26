"""`ats-jobs-scraper` — the MVP Actor (SPEC v2 §4, §5.12, §8, §13.4, §14).

Pipeline, in order, and everything before the last step is free:

    input (+ key aliases, §4.1) -> resolve (§5.11) -> adapter fetch (§5) -> description /
    redaction / raw post-processing (§4.1) -> filters (§4.1) -> dedupe (§4.5.6) ->
    onlyNewJobs delta (§4.5.6) -> push, charging `job` once per delivered row (§8.2) ->
    free company_summary and error rows (§4.6)

The rules this file exists to enforce (§8.2, §5.12, §10.1 `test_billing.py`):

* `job` is the only paid event, charged once per delivered row — `core.billing` owns
  every `push_data` call so that stays true.
* Company summaries, error rows, filtered-out jobs, failed companies and companies never
  reached because `maxJobs` or the charge limit fired are all free.
* `delta-run` is charged once per run when `onlyNewJobs` is on, never otherwise.
* One bad company never ends the run: it becomes a typed `error` row (§13.4 #14).
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

from apify import Actor

from core.billing import Billing
from core.directory import get_directory
from core.filters import Filters
from core.http import FetchError, make_client
from core.models import PROVIDERS, Ref
from core.normalize.redact import redact_description
from core.providers import AdapterNotFound, get_adapter
from core.resolve import Unresolved, needs_directory, resolve
from core.state import SeenState

#: §8.6 — anti-abuse: no single company may hold a run hostage.
COMPANY_BUDGET_SECS = 120.0

#: §4.1 input-key aliases, so a saved input from a competitor Actor runs here unchanged.
INPUT_ALIASES = (
    "queries",
    "companyUrls",
    "startUrls",
    "boardTokens",
    "siteNames",
    "jobBoardNames",
    "subdomains",
    "companyIdentifiers",
)

#: Every `.actor/input_schema.json` default, restated so the Actor behaves identically
#: when it is called through the API with a partial input.
DEFAULTS: dict[str, Any] = {
    "providers": list(PROVIDERS),
    "maxJobs": 1000,
    "maxJobsPerCompany": 0,
    "titleKeywords": [],
    "excludeTitleKeywords": [],
    "locationKeywords": [],
    "remoteOnly": False,
    "departments": [],
    "employmentTypes": [],
    "strictEmploymentType": False,
    "postedAfter": None,
    "includeDescription": False,
    "descriptionFormat": "text",
    "redactContacts": True,
    "outputProfile": "full",
    "includeCompanySummary": True,
    "includeRawJson": False,
    "dedupe": "id",
    "onlyNewJobs": False,
    "stateKey": "ats-jobs-state-default",
    "stateRetentionDays": 90,
    "maxConcurrency": 8,
    "requestTimeoutSecs": 30,
    "failOnAllErrors": False,
}

#: Company-level state, beside `core.state`'s job ids in the same named KV store.
COMPANY_STATE_KEY = "companies"

#: §5.12 provider-degradation guard. The spec's rule is ">90% of companies for one
#: provider return 200-with-zero-jobs"; the minimum sample is added because on a
#: one-company run a genuinely empty board is 100% and would libel the provider.
DEGRADED_RATIO = 0.9
DEGRADED_MIN_COMPANIES = 5


# --------------------------------------------------------------------------- input


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Apply defaults without letting a meaningful falsy value (`maxJobs: 0`) be lost."""
    cfg = {
        key: (raw[key] if raw.get(key) is not None else value) for key, value in DEFAULTS.items()
    }
    cfg["providers"] = [p for p in cfg["providers"] if p in PROVIDERS] or list(PROVIDERS)
    cfg["maxConcurrency"] = max(1, int(cfg["maxConcurrency"]))
    return cfg


def read_companies(raw: dict[str, Any]) -> list[str]:
    """`companies`, or the first non-empty §4.1 alias. Order preserved, repeats dropped."""
    values = raw.get("companies") or next(
        (raw[alias] for alias in INPUT_ALIASES if raw.get(alias)), []
    )
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for value in values:
        if isinstance(value, dict):  # `startUrls` arrive as [{"url": ...}]
            value = value.get("url") or value.get("value") or value.get("slug")
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------- data rows


def error_item(
    provider: str | None, slug: str | None, entry: str | None, status: str, message: str
) -> dict[str, Any]:
    """A free `error` row (§4.6). Typed status, human message — never a stack trace."""
    return {
        "recordType": "error",
        "provider": provider,
        "companySlug": slug,
        "status": status,
        "error": message,
        "scrapedAt": now_iso(),
        "input": entry,
    }


def summary_item(
    ref: Ref,
    *,
    status: str,
    company: str | None = None,
    domain: str | None = None,
    jobs_found: int | None = None,
    jobs_kept: int | None = None,
    new_jobs: int | None = None,
    duplicates: int | None = None,
    tracked_since: str | None = None,
    top_departments: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """A free `company_summary` row (§4.6). Never charged, ever."""
    return {
        "recordType": "company_summary",
        "provider": ref.provider,
        "companySlug": ref.slug,
        "company": company,
        "companyDomain": domain,
        "status": status,
        "jobsFound": jobs_found,
        "jobsKept": jobs_kept,
        "newJobs": new_jobs,
        "duplicatesDropped": duplicates,
        "trackedSince": tracked_since,
        "topDepartments": top_departments,
        "scrapedAt": now_iso(),
        "input": ref.input,
        "warnings": warnings or None,
    }


def top_departments(records: list[Any], limit: int = 3) -> list[dict[str, Any]] | None:
    counts = Counter(r.department for r in records if r.department)
    return [{"name": name, "count": count} for name, count in counts.most_common(limit)] or None


def classify(exc: BaseException) -> tuple[str, str]:
    """Map an adapter failure onto a §5.12 status plus a message a buyer can act on."""
    if isinstance(exc, FetchError):
        return exc.status, str(exc)
    if isinstance(exc, TimeoutError):
        return "timeout", f"the company took longer than {COMPANY_BUDGET_SECS:.0f}s"
    if isinstance(exc, ValueError | KeyError | TypeError):
        return "parse_error", f"the provider payload did not parse: {type(exc).__name__}: {exc}"
    return "http_error", f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------------ record shaping


def shape_record(record: Any, cfg: dict[str, Any]) -> None:
    """Apply the §4.1 output switches: description, format, redaction, raw payload.

    Redaction is re-applied here rather than trusted to the adapter: it is the mechanism
    behind the §15.2 no-PII claim, and `redact_text` on already-clean text is a no-op.
    """
    if not cfg["includeDescription"]:
        record.descriptionHtml = None
        record.descriptionText = None
        record.descriptionRedacted = None
    else:
        if cfg["descriptionFormat"] == "text":
            record.descriptionHtml = None
        elif cfg["descriptionFormat"] == "html":
            record.descriptionText = None
        if record.descriptionHtml or record.descriptionText:
            html, text, redacted = redact_description(
                record.descriptionHtml, record.descriptionText, cfg["redactContacts"]
            )
            record.descriptionHtml, record.descriptionText = html, text
            if redacted is not None:
                record.descriptionRedacted = bool(record.descriptionRedacted) or redacted
    if not cfg["includeRawJson"]:
        record.raw = None


async def adapter_fetch(module: ModuleType, ref: Ref, client: Any, cfg: dict[str, Any]) -> list:
    """Call `fetch(ref, client)`, passing the input dict too when the adapter takes it."""
    params = inspect.signature(module.fetch).parameters
    if "options" in params:
        result = module.fetch(ref, client, options=cfg)
    elif len(params) >= 3:
        result = module.fetch(ref, client, cfg)
    else:
        result = module.fetch(ref, client)
    return list(await result) if inspect.isawaitable(result) else list(result)


# -------------------------------------------------------------------- run context


@dataclass(slots=True)
class RunCtx:
    cfg: dict[str, Any]
    filters: Filters
    billing: Billing
    state: SeenState | None = None
    company_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    seen_ids: set[str] = field(default_factory=set)
    seen_content: set[str] = field(default_factory=set)
    summaries: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    companies_seen: Counter = field(default_factory=Counter)
    companies_zero: Counter = field(default_factory=Counter)
    jobs_found_total: int = 0
    companies_ok: int = 0


def company_key(ref: Ref) -> str:
    """Lower-cased for lookup only — a fetch URL is never rebuilt from it (§5.11)."""
    return f"{ref.provider}:{ref.slug.casefold()}"


# ------------------------------------------------------------------ the pipeline


def dedupe(ctx: RunCtx, records: list[Any]) -> tuple[list[Any], int]:
    """§4.5.6: always by `id`, additionally by `contentKey` when asked. Run-wide, and the
    surviving row carries the ids it swallowed in `dedupedFrom`."""
    kept: list[Any] = []
    by_content: dict[str, Any] = {}
    dropped = 0
    for record in records:
        if record.id:
            if record.id in ctx.seen_ids:
                dropped += 1
                continue
            ctx.seen_ids.add(record.id)
        if ctx.cfg["dedupe"] == "content" and record.contentKey:
            survivor = by_content.get(record.contentKey)
            if survivor is not None:
                survivor.dedupedFrom = (survivor.dedupedFrom or []) + [record.id or ""]
                dropped += 1
                continue
            if record.contentKey in ctx.seen_content:
                dropped += 1
                continue
            ctx.seen_content.add(record.contentKey)
            by_content[record.contentKey] = record
        kept.append(record)
    return kept, dropped


def apply_delta(ctx: RunCtx, records: list[Any]) -> tuple[list[Any], int | None]:
    """§4.5.6 across runs. Without a state store `isNew` stays null — we do not know."""
    if ctx.state is None:
        return records, None
    new_jobs = 0
    for record in records:
        if not record.id:
            continue
        record.isNew = ctx.state.is_new(record.id)
        record.firstSeenAt = ctx.state.first_seen(record.id) or record.scrapedAt
        if ctx.state.mark(record.id, record.changeHash):
            new_jobs += 1
    return [r for r in records if r.isNew], new_jobs


async def push_jobs(ctx: RunCtx, records: list[Any]) -> int:
    """One `job` event per delivered row; `core.billing` decides when to stop (§8.2)."""
    profile = ctx.cfg["outputProfile"]
    delivered = 0
    async with ctx.lock:
        for record in records:
            if not await ctx.billing.push_job(record.to_item(profile)):
                break
            delivered += 1
    return delivered


async def process_company(ref: Ref, client: Any, ctx: RunCtx) -> None:
    cfg = ctx.cfg
    started = time.monotonic()
    try:
        module = get_adapter(ref.provider)
        async with asyncio.timeout(COMPANY_BUDGET_SECS):
            records = await adapter_fetch(module, ref, client, cfg)
    except AdapterNotFound as exc:
        await ctx.billing.push_free(
            error_item(ref.provider, ref.slug, ref.input, "provider_unavailable", str(exc))
        )
        Actor.log.warning("adapter missing", extra={"provider": ref.provider, "slug": ref.slug})
        return
    except Exception as exc:  # noqa: BLE001 - one bad company never ends the run (§13.5)
        status, message = classify(exc)
        await ctx.billing.push_free(error_item(ref.provider, ref.slug, ref.input, status, message))
        Actor.log.warning(
            "company failed",
            extra={"provider": ref.provider, "slug": ref.slug, "status": status},
        )
        return

    ctx.companies_seen[ref.provider] += 1
    ctx.companies_ok += 1
    jobs_found = len(records)
    ctx.jobs_found_total += jobs_found
    if jobs_found == 0:
        ctx.companies_zero[ref.provider] += 1

    company = next((r.company for r in records if r.company), None)
    domain = next((r.companyDomain for r in records if r.companyDomain), None)
    scraped = now_iso()

    survivors: list[Any] = []
    for record in records:
        record.provider = record.provider or ref.provider
        record.companySlug = record.companySlug or ref.slug
        record.input = record.input or ref.input
        record.scrapedAt = record.scrapedAt or scraped
        shape_record(record, cfg)
        if ctx.filters.keep(record):
            survivors.append(record)

    async with ctx.lock:
        kept, duplicates = dedupe(ctx, survivors)
        emit, new_jobs = apply_delta(ctx, kept)
    if cfg["maxJobsPerCompany"]:
        emit = emit[: cfg["maxJobsPerCompany"]]

    delivered = await push_jobs(ctx, emit)

    warnings = list(ctx.filters.warnings)
    tracked_since = None
    if ctx.state is not None:
        key = company_key(ref)
        previous = ctx.company_state.get(key, {})
        tracked_since = previous.get("firstSeen") or scraped[:10]
        # §5.12 empty-board guard: a board that had jobs last run and none now is
        # suspect, never proof that the company stopped hiring.
        if jobs_found == 0 and previous.get("jobs"):
            warnings.append("empty_suspect")
        ctx.company_state[key] = {
            "firstSeen": tracked_since,
            "lastSeen": scraped[:10],
            "jobs": jobs_found,
        }

    status = ctx.billing.stop_status if delivered < len(emit) else "ok"
    ctx.summaries.append(
        (
            ref.provider,
            summary_item(
                ref,
                status=status or "ok",
                company=company,
                domain=domain,
                jobs_found=jobs_found,
                jobs_kept=len(kept),
                new_jobs=new_jobs,
                duplicates=duplicates,
                tracked_since=tracked_since,
                top_departments=top_departments(kept),
                warnings=warnings,
            ),
        )
    )
    Actor.log.info(
        "company done",
        extra={
            "provider": ref.provider,
            "slug": ref.slug,
            "jobsFound": jobs_found,
            "jobsKept": len(kept),
            "jobsDelivered": delivered,
            "newJobs": new_jobs,
            "duplicatesDropped": duplicates,
            "seconds": round(time.monotonic() - started, 2),
        },
    )


async def worker(queue: asyncio.Queue[Ref], client: Any, ctx: RunCtx) -> None:
    while True:
        try:
            ref = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        try:
            if ctx.billing.stopped:
                # §5.12: companies past the cutoff are never fetched, so never charged.
                ctx.summaries.append(
                    (ref.provider, summary_item(ref, status=ctx.billing.stop_status or "ok"))
                )
                continue
            await process_company(ref, client, ctx)
        finally:
            queue.task_done()


async def resolve_all(entries: list[str], ctx: RunCtx) -> list[Ref]:
    """§5.11. The directory is loaded only if some entry actually needs it (§6.6)."""
    directory = None
    if any(needs_directory(entry) for entry in entries):
        try:
            directory = await get_directory()
        except Exception as exc:  # noqa: BLE001 - a missing directory is not a failed run
            Actor.log.warning("company directory unavailable", extra={"error": str(exc)})

    refs: list[Ref] = []
    for entry in entries:
        result = resolve(entry, providers=ctx.cfg["providers"], directory=directory)
        if isinstance(result, Unresolved):
            await ctx.billing.push_free(
                error_item(None, None, entry, result.status, result.message)
            )
            continue
        if result.provider not in ctx.cfg["providers"]:
            await ctx.billing.push_free(
                error_item(
                    result.provider,
                    result.slug,
                    entry,
                    "not_found",
                    f"{result.provider} is not in the providers you selected",
                )
            )
            continue
        refs.append(result)
    return refs


async def flush_summaries(ctx: RunCtx) -> None:
    """§5.12 degradation guard, then push every free summary row."""
    degraded = {
        provider
        for provider, seen in ctx.companies_seen.items()
        if seen >= DEGRADED_MIN_COMPANIES and ctx.companies_zero[provider] / seen > DEGRADED_RATIO
    }
    for provider in degraded:
        Actor.log.error(
            "provider degraded: 200-with-zero-jobs at population scale — see §14.3",
            extra={"provider": provider, "companies": ctx.companies_seen[provider]},
        )
    if not ctx.cfg["includeCompanySummary"]:
        return
    for provider, item in ctx.summaries:
        if provider in degraded:
            item["warnings"] = (item.get("warnings") or []) + ["provider_degraded"]
        await ctx.billing.push_free(item)


async def main() -> None:
    async with Actor:
        raw = await Actor.get_input() or {}
        cfg = read_config(raw)
        entries = read_companies(raw)
        ctx = RunCtx(
            cfg=cfg,
            filters=Filters.from_input(cfg),
            billing=Billing(max_jobs=int(cfg["maxJobs"] or 0)),
        )

        Actor.log.info(
            "run start",
            extra={
                "companies": len(entries),
                "providers": cfg["providers"],
                "maxJobs": cfg["maxJobs"],
                "onlyNewJobs": cfg["onlyNewJobs"],
                "outputProfile": cfg["outputProfile"],
                "filters": ctx.filters.active,
            },
        )

        if not entries:
            await ctx.billing.push_free(
                error_item(
                    None,
                    None,
                    None,
                    "no_companies",
                    "No companies given. Add slugs or career-site URLs to the Companies field.",
                )
            )
            return

        refs = await resolve_all(entries, ctx)
        if not refs:
            Actor.log.warning("nothing resolved", extra={"entries": len(entries)})
            if cfg["failOnAllErrors"]:
                await Actor.fail(status_message="No company could be resolved to an ATS board")
            return

        store = None
        if cfg["onlyNewJobs"]:
            # §8.2: instrumentation only, $0.00, once per run, never otherwise.
            await ctx.billing.charge_delta_run()
            ctx.state = await SeenState.open(cfg["stateKey"])
            store = await Actor.open_key_value_store(name=cfg["stateKey"])
            ctx.company_state = await store.get_value(COMPANY_STATE_KEY) or {}

        async with make_client(timeout_secs=float(cfg["requestTimeoutSecs"])) as client:
            queue: asyncio.Queue[Ref] = asyncio.Queue()
            for ref in refs:
                queue.put_nowait(ref)
            await asyncio.gather(
                *(
                    asyncio.create_task(worker(queue, client, ctx))
                    for _ in range(min(cfg["maxConcurrency"], len(refs)))
                )
            )

        await flush_summaries(ctx)
        if ctx.state is not None and store is not None:
            pruned = ctx.state.prune(int(cfg["stateRetentionDays"]))
            await ctx.state.save()
            await store.set_value(COMPANY_STATE_KEY, ctx.company_state)
            Actor.log.info(
                "state saved",
                extra={"ids": len(ctx.state.seen), "pruned": pruned, "key": cfg["stateKey"]},
            )

        Actor.log.info(
            "run done",
            extra={
                "companies": len(refs),
                "companiesOk": ctx.companies_ok,
                "jobsFound": ctx.jobs_found_total,
                "jobsPushed": ctx.billing.jobs_pushed,
                "freeRows": ctx.billing.free_pushed,
                "stopped": ctx.billing.stop_status,
            },
        )
        await Actor.set_status_message(
            f"{ctx.billing.jobs_pushed} job rows from {ctx.companies_ok}/{len(refs)} companies"
            + (f" ({ctx.billing.stop_status})" if ctx.billing.stop_status else "")
        )
        if cfg["failOnAllErrors"] and ctx.jobs_found_total == 0:
            await Actor.fail(status_message="Every company failed or returned no jobs")


if __name__ == "__main__":
    asyncio.run(main())
