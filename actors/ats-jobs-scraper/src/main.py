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

from apify import Actor, Event

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

#: `.actor/input_schema.json` -> `companies.maxItems`, restated for API callers (V1 L4).
MAX_COMPANIES = 2000

#: Headroom left inside `COMPANY_BUDGET_SECS` for an adapter to wind down and emit what
#: it already has, instead of being cancelled with the whole company in a buffer (V1 H1).
BUDGET_HEADROOM_SECS = 10.0


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
    # `maxItems: 2000` is only enforced by the Console form; an API caller can queue
    # 50,000 (V1 L4). `maxJobs` bounds the charge, not the CU burn.
    return list(dict.fromkeys(out))[:MAX_COMPANIES]


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
        return "parse_error", f"the provider payload did not parse: {type(exc).__name__}"
    # V3 S13: an unexpected exception's message can carry internal paths and state, and
    # the `error` row is customer-visible. The detail goes to the log, not the dataset.
    return "http_error", f"the company could not be fetched ({type(exc).__name__})"


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
    #: contentKey -> the row that survived it, run-wide. A per-company map recorded
    #: `dedupedFrom` inside a company but dropped a cross-company collision with no
    #: trace at all (§4.5.6 correction 2, V1 M6).
    content_survivors: dict[str, Any] = field(default_factory=dict)
    summaries: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    companies_seen: Counter = field(default_factory=Counter)
    companies_zero: Counter = field(default_factory=Counter)
    jobs_found_total: int = 0
    companies_ok: int = 0
    store: Any = None
    #: Idempotence for `flush_summaries`, which the abort handler may reach first (V1 M4).
    flushed: bool = False


def company_key(ref: Ref) -> str:
    """Lower-cased for lookup only — a fetch URL is never rebuilt from it (§5.11)."""
    return f"{ref.provider}:{ref.slug.casefold()}"


# ------------------------------------------------------------------ the pipeline


def dedupe(ctx: RunCtx, records: list[Any]) -> tuple[list[Any], int]:
    """§4.5.6: always by `id`, additionally by `contentKey` when asked. Run-wide, and the
    surviving row carries the ids it swallowed in `dedupedFrom`."""
    kept: list[Any] = []
    dropped = 0
    for record in records:
        if record.id:
            if record.id in ctx.seen_ids:
                dropped += 1
                continue
            ctx.seen_ids.add(record.id)
        if ctx.cfg["dedupe"] == "content" and record.contentKey:
            survivor = ctx.content_survivors.get(record.contentKey)
            if survivor is not None:
                # The survivor may already have been pushed, in which case the merge is
                # only visible in `duplicatesDropped` on the summary — documented in
                # `dataset_schema.json` -> `dedupedFrom` (V1 M6).
                survivor.dedupedFrom = (survivor.dedupedFrom or []) + [record.id or ""]
                dropped += 1
                continue
            ctx.content_survivors[record.contentKey] = record
        kept.append(record)
    return kept, dropped


def apply_delta(ctx: RunCtx, records: list[Any]) -> tuple[list[Any], int | None]:
    """§4.5.6 across runs. Without a state store `isNew` stays null — we do not know.

    Decides only. Marking here used to happen for every kept row — before the
    `maxJobsPerCompany` trim, before `maxJobs` and before the charge limit could stop the
    push — so a row that was never delivered still had `isNew=false` on every later run
    and was lost permanently (V1 B3). :func:`commit_delta` marks what actually landed.
    """
    if ctx.state is None:
        return records, None
    for record in records:
        if not record.id:
            continue
        record.isNew = ctx.state.is_new(record.id)
        record.firstSeenAt = ctx.state.first_seen(record.id) or record.scrapedAt
    return [r for r in records if r.isNew], 0


def commit_delta(ctx: RunCtx, delivered: list[Any]) -> int:
    """Mark the rows that were actually pushed. Never call it before the push (V1 B3)."""
    if ctx.state is None:
        return 0
    marked = 0
    for record in delivered:
        if record.id and ctx.state.mark(record.id, record.changeHash):
            marked += 1
    return marked


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
    # §8.6's budget as a deadline the adapter can *see*, not only as a killer: Rippling
    # issues one detail call per job against a 2 rps bucket, so a 374-job board overran
    # 120 s and the cancellation cost the buyer every row (V1 H1).
    budget = dict(cfg, deadline=started + COMPANY_BUDGET_SECS - BUDGET_HEADROOM_SECS)
    try:
        module = get_adapter(ref.provider)
        async with asyncio.timeout(COMPANY_BUDGET_SECS):
            records = await adapter_fetch(module, ref, client, budget)
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
            extra={
                "provider": ref.provider,
                "slug": ref.slug,
                "status": status,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return

    ctx.companies_seen[ref.provider] += 1
    ctx.companies_ok += 1
    jobs_found = len(records)
    ctx.jobs_found_total += jobs_found
    if jobs_found == 0:
        ctx.companies_zero[ref.provider] += 1

    company = next((r.company for r in records if r.company), None)
    domain = next((r.companyDomain for r in records if r.companyDomain), None) or ref.domain
    scraped = now_iso()

    survivors: list[Any] = []
    for record in records:
        record.provider = record.provider or ref.provider
        record.companySlug = record.companySlug or ref.slug
        record.input = record.input or ref.input
        record.scrapedAt = record.scrapedAt or scraped
        # §4.6 advertises `companyDomain`; no adapter can supply it, the directory row
        # can (V1 M3).
        record.companyDomain = record.companyDomain or ref.domain
        shape_record(record, cfg)
        if ctx.filters.keep(record):
            survivors.append(record)

    async with ctx.lock:
        kept, duplicates = dedupe(ctx, survivors)
        emit, new_jobs = apply_delta(ctx, kept)
    if cfg["maxJobsPerCompany"]:
        emit = emit[: cfg["maxJobsPerCompany"]]

    try:
        delivered = await push_jobs(ctx, emit)
    except Exception as exc:  # noqa: BLE001 - a push failure is this company's, not the run's
        status, message = classify(exc)
        await ctx.billing.push_free(error_item(ref.provider, ref.slug, ref.input, status, message))
        Actor.log.warning(
            "push failed",
            extra={"provider": ref.provider, "slug": ref.slug, "error": f"{type(exc)}: {exc}"},
        )
        return
    if ctx.state is not None:
        async with ctx.lock:
            # §4.5.6, V1 B3: state records what was delivered, never what was considered.
            new_jobs = commit_delta(ctx, emit[:delivered])

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


async def resolve_all(entries: list[str], client: Any, ctx: RunCtx) -> list[Ref]:
    """§5.11. The directory is loaded only if some entry actually needs it (§6.6).

    ``client`` is not optional: `load_directory` only offers the jsDelivr and
    raw.githubusercontent sources when it has one, so calling `get_directory()` bare left
    the loader with nothing but an empty KV store and a baked file that is not in the tree
    — every bare slug and company name became an error row (V1 H2, V3 S10).
    """
    directory = None
    if any(needs_directory(entry) for entry in entries):
        try:
            directory = await get_directory(client)
        except Exception as exc:  # noqa: BLE001 - a missing directory is not a failed run
            Actor.log.warning("company directory unavailable", extra={"error": str(exc)})

    refs: list[Ref] = []
    for entry in entries:
        # `providers` restricts directory lookups only; an explicit prefix or URL always
        # wins (§4.1, §5.11). `resolve()` implements that — re-filtering the result here
        # broke every saved input carrying both a URL list and a narrowed provider list,
        # and blamed the user's slug for it (V1 H3).
        result = resolve(entry, providers=ctx.cfg["providers"], directory=directory)
        if isinstance(result, Unresolved):
            await ctx.billing.push_free(
                error_item(None, None, entry, result.status, result.message)
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
    if ctx.flushed:  # the ABORTING handler may have run already (V1 M4)
        return
    ctx.flushed = True
    if not ctx.cfg["includeCompanySummary"]:
        return
    for provider, item in ctx.summaries:
        if provider in degraded:
            item["warnings"] = (item.get("warnings") or []) + ["provider_degraded"]
            # §5.12: "rather than a per-company `ok`" — a pipeline filtering on
            # `status == "ok"` must not record a broken provider as healthy (V1 M1).
            if item.get("status") == "ok":
                item["status"] = "provider_degraded"
        await ctx.billing.push_free(item)


async def open_state(ctx: RunCtx) -> None:
    """`onlyNewJobs` state, degrading instead of failing the run (V1 M5).

    Apify rejects a named store whose name it does not like, and that rejection used to
    surface as an unhandled exception *after* every company had already resolved — the
    opposite of the "degrade, never fail" posture the rest of the shell keeps.
    """
    key = ctx.cfg["stateKey"]
    try:
        ctx.state = await SeenState.open(key)
        ctx.store = await Actor.open_key_value_store(name=key)
        previous = await ctx.store.get_value(COMPANY_STATE_KEY)
    except Exception as exc:  # noqa: BLE001
        ctx.state = None
        ctx.store = None
        Actor.log.warning(
            "state store unavailable; onlyNewJobs disabled", extra={"error": str(exc)}
        )
        await ctx.billing.push_free(
            error_item(None, None, None, "http_error", f"could not open state store {key!r}")
        )
        return
    # V3 S14: a non-dict value in the store used to crash later at `.get`.
    ctx.company_state = previous if isinstance(previous, dict) else {}


async def save_state(ctx: RunCtx) -> None:
    if ctx.state is None or ctx.store is None:
        return
    pruned = ctx.state.prune(int(ctx.cfg["stateRetentionDays"]))
    await ctx.state.save()
    await ctx.store.set_value(COMPANY_STATE_KEY, ctx.company_state)
    Actor.log.info(
        "state saved",
        extra={"ids": len(ctx.state.seen), "pruned": pruned, "key": ctx.cfg["stateKey"]},
    )


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

        # §5.12's last row: flush what we have inside the 30 s abort/migration window,
        # or an aborted monitoring run loses every summary *and* its delta baseline (M4).
        async def on_abort(_data: Any = None) -> None:
            await flush_summaries(ctx)
            await save_state(ctx)

        Actor.on(Event.ABORTING, on_abort)
        Actor.on(Event.MIGRATING, on_abort)

        async with make_client(timeout_secs=float(cfg["requestTimeoutSecs"])) as client:
            refs = await resolve_all(entries, client, ctx)
            if not refs:
                Actor.log.warning("nothing resolved", extra={"entries": len(entries)})
                if cfg["failOnAllErrors"]:
                    await Actor.fail(status_message="No company could be resolved to an ATS board")
                return

            if cfg["onlyNewJobs"]:
                # §8.2: instrumentation only, $0.00, once per run, never otherwise.
                await ctx.billing.charge_delta_run()
                await open_state(ctx)

            queue: asyncio.Queue[Ref] = asyncio.Queue()
            for ref in refs:
                queue.put_nowait(ref)
            # `return_exceptions=True`: a raise out of one worker used to orphan its
            # siblings, close the HTTP client under them and skip both the summary flush
            # and the state save — losing the ids of rows already charged for (V3 S9).
            outcomes = await asyncio.gather(
                *(
                    asyncio.create_task(worker(queue, client, ctx))
                    for _ in range(min(cfg["maxConcurrency"], len(refs)))
                ),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    Actor.log.exception("worker crashed", exc_info=outcome)

        await flush_summaries(ctx)
        await save_state(ctx)

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
