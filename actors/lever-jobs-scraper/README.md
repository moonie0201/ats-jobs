# Lever Jobs API Scraper

> **Unofficial.** Not affiliated with, endorsed by or sponsored by Lever, Inc.
> It calls Lever's own public postings API. All trademarks belong to their owners.
> **Removal requests:** [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md)
> — honoured in 48 hours. **Privacy:** [PRIVACY.md](https://github.com/moonie0201/ats-jobs/blob/main/PRIVACY.md).

Live job postings straight from the **Lever public postings API** — `api.lever.co/v0/postings/{site}` — normalized into one flat schema with structured salary, a real remote flag, parsed locations and a stable job id. You paste Lever site names (`palantir`) or Lever careers URLs; you get clean job rows back. Seven filters run **before** anything is billed, company summaries and error rows are free, and `onlyNewJobs` keeps a baseline in your own key-value store so a daily monitoring run returns only what changed. No scraping, no headless browser, no proxies, no API keys.

**We index nothing you buy.** There is no "jobs in our database" number here, because there is no index between you and the board: every row in your run was fetched from that employer's Lever site seconds earlier, never served from a crawl of unknown age. And because you are billed only for job rows you actually receive, a 500-company daily watch with `onlyNewJobs` costs a couple of cents a run.

**What this is not:** no keyword search without a company list; and this listing reads **Lever only** — an entry for another ATS comes back as a free error row.

## What this Lever jobs API does

- **Endpoint** — [`api.lever.co/v0/postings/{site}?mode=json`](https://github.com/lever/postings-api), with the EU host `api.eu.lever.co` tried automatically when the global host has nothing
- **Site name** — the slug in your careers URL: `https://jobs.lever.co/palantir` → `palantir`. **Case-sensitive**: `palantir` works, `Palantir` returns 404
- **Remote flag** — Lever's `workplaceType` becomes a real `remote` boolean with `remoteSource: "ats"`
- **Salary** — Lever's `salaryRange` becomes `salaryMin`, `salaryMax`, `salaryCurrency`, `salaryInterval` with `salarySource: "ats"` where the employer filled it in
- **Descriptions** — the full ad body, inline in the same response, on by default at no extra cost

That endpoint is **Lever's own public postings API**: the same one a company's careers page calls to render itself, published by Lever and requiring no credential. That is a different product from career-page scraping — no headless browser, no residential proxy, no bot walls, nothing that breaks when a marketing site is restyled.

Rows you are **not** charged for: company summaries, error rows, jobs removed by your filters, companies never reached because your `maxJobs` cap fired, and companies that failed.

## Supported endpoint and what Lever actually gives you

| Field | Lever | Note |
|---|---|---|
| Remote flag | **Yes** — `workplaceType` | One of the two providers in this family that states it outright |
| Structured salary | Sometimes — `salaryRange` | Present only where the employer filled it in; regex fallback otherwise |
| Description | **Yes**, inline | Lists and closing sections are joined into one body |
| Employment type | **Yes** — `commitment` | Normalized to `full_time` / `part_time` / `contract` / `temporary` / `internship` / `other`, `employmentTypeSource: "ats"` |
| Department / team | **Yes** — categories | `department` and `team` both populated where Lever has them |
| Job URL | Yes | `hostedUrl`, plus `applyUrl` where Lever exposes one |
| Posted date | **Yes** — `createdAt` | Exposed as `postedAt` with `postedAtSource` |
| EU hosting | **Yes** | EU-resident boards live on `api.eu.lever.co`; we try both and report which answered |

## How to use the Lever postings API

1. **Paste your site names.** One per line: `palantir`, or the full URL `https://jobs.lever.co/palantir`. EU URLs (`jobs.eu.lever.co/...`) resolve too. **Watch the casing** — Lever site names are case-sensitive.
2. **Set your filters.** Title keywords, excluded titles, location, remote-only, departments, employment types, posted-after. They all run locally on the fetched JSON, before billing.
3. **Run it, or schedule it.** For monitoring, turn on `onlyNewJobs` and give the task its own `stateKey`; the first run stores the baseline and later runs return only what is new.

A minimal run — `["palantir"]` with `maxJobs: 50` — finishes in seconds and costs ten cents.

## Coming from fantastic-jobs, jobo, bovi or webdata_labs

Saved input from another Lever Actor runs here unchanged. These input keys are accepted as aliases for `companies`, first non-empty wins, and an explicit `companies` always wins over an alias:

| Their key | Here |
|---|---|
| `siteNames`, `boardTokens`, `jobBoardNames` | `companies` |
| `queries`, `companyUrls`, `startUrls` | `companies` |
| `subdomains`, `companyIdentifiers` | `companies` |

**What you gain:** live boards instead of an index of unknown age; `postedAfter` and per-company filtering that run *before* billing, so filtered-out jobs are free; structured salary with currency and interval separated rather than a pay string; the EU host tried automatically instead of a silent 404; no contact, recruiter or candidate fields anywhere in the output; the full ad body on by default with contact redaction on by default; and a `trackedSince` date on every company row once `onlyNewJobs` is on.

**What you lose, stated plainly:** there is no keyword search without a company list. If "find me every Rust job anywhere" is your requirement, an aggregator fits better than this Actor. And this listing is Lever-only by design — if you need Greenhouse, Ashby, Recruitee, Rippling or Personio in the same run, use the multi-ATS Actor `ats-jobs-scraper` instead of running four listings.

## Input

```json
{
  "companies": ["palantir", "https://jobs.lever.co/matchgroup"],
  "maxJobs": 1000,
  "titleKeywords": ["engineer", "data"],
  "excludeTitleKeywords": ["intern"],
  "locationKeywords": ["Berlin", "Germany", "Remote"],
  "remoteOnly": false,
  "employmentTypes": ["full_time"],
  "postedAfter": "7 days",
  "includeDescription": true,
  "descriptionFormat": "text",
  "redactContacts": true,
  "outputProfile": "full",
  "onlyNewJobs": false,
  "stateKey": "lever-jobs-state-default"
}
```

| Field | Type | Default | Effect on cost |
|---|---|---|---|
| `companies` | array | required | Site names or careers URLs. More companies, more charged rows |
| `maxJobs` | integer | 1000 | **Your main cost control.** Hard stop after this many charged rows; `0` = no limit |
| `maxJobsPerCompany` | integer | 0 | Stops one enterprise board eating the whole budget |
| `titleKeywords` / `excludeTitleKeywords` | array | `[]` | Filtered-out jobs are free |
| `locationKeywords` | array | `[]` | Matches raw location text and parsed city, region, country |
| `remoteOnly` | boolean | false | Keeps only `remote: true` — reliable on Lever, which states it |
| `departments` | array | `[]` | Substring match on the Lever category |
| `employmentTypes` | array | `[]` | Lever reports a commitment on most postings |
| `strictEmploymentType` | boolean | false | Keeps only ATS-confirmed types — usually safe here |
| `postedAfter` | string | null | `2026-08-01` or `7 days`. Jobs with no date are **kept** |
| `includeDescription` | boolean | **true** | Bigger items, same price — the body costs nothing extra |
| `redactContacts` | boolean | true | Strips contact details from description bodies |
| `outputProfile` | string | `full` | `minimal` cuts token cost for AI agents |
| `dedupe` | string | `id` | `content` also merges same title + company + location + requisition id |
| `onlyNewJobs` | boolean | false | Returns only ids not in your state store |
| `stateKey` | string | `lever-jobs-state-default` | Name of the key-value store holding seen ids |
| `maxConcurrency` | integer | 8 | Speed only; `api.lever.co` is capped separately at 1 rps |

## Output

One `job` row per posting (charged), plus free `company_summary` and `error` rows. The dataset schema is identical to the multi-ATS Actor's, so a pipeline can read both without a branch.

```json
{
  "recordType": "job",
  "id": "lever:palantir:6e1f0c11-1c58-4f3b-9d0e-2a7c1f0a1234",
  "provider": "lever",
  "companySlug": "palantir",
  "company": "Palantir Technologies",
  "title": "Backend Engineer, Foundry",
  "department": "Engineering",
  "team": "Foundry",
  "locationRaw": "London, UK",
  "city": "London",
  "country": "United Kingdom",
  "countryCode": "GB",
  "remote": false,
  "workplaceType": "onsite",
  "remoteSource": "ats",
  "employmentType": "full_time",
  "employmentTypeSource": "ats",
  "salaryMin": null,
  "salaryMax": null,
  "url": "https://jobs.lever.co/palantir/6e1f0c11-1c58-4f3b-9d0e-2a7c1f0a1234",
  "postedAt": "2026-08-19T00:00:00Z",
  "postedAtSource": "created_at",
  "descriptionRedacted": false,
  "isNew": true,
  "scrapedAt": "2026-08-26T03:00:12Z"
}
```

Two dataset views are provided: **Jobs** (spreadsheet-ready postings) and **Company summaries**. Export either as JSON, CSV, Excel or XML from the Storage tab.

**What `null` means: we could not determine it.** We never guess remote status, employment type, seniority or salary. A null field is the honest answer, and far more useful than an invented one when you are filtering thousands of rows.

## Filters, deduplication and normalization

- **`remote`** comes from Lever's `workplaceType` where present (`remoteSource: "ats"`); otherwise from the location text or the title, and `null` when nothing said. `remoteOnly` keeps only positively-confirmed remote jobs.
- **Salary** comes from `salaryRange` first (`salarySource: "ats"`). Only when there is none do we run a conservative regex over the salary text (`salarySource: "parsed"`), with rejection gates for equity, bonuses, funding rounds, `401(k)`, date ranges and phone numbers. **Never an LLM.**
- **Employment type** is normalized from Lever's `commitment` to `full_time`, `part_time`, `contract`, `temporary`, `internship` or `other`. `strictEmploymentType` keeps only ATS-confirmed ones.
- **Dedupe by id** is always on. **By content** additionally merges rows with the same title, company, raw location and requisition id — useful for boards that list a role once per office, but it can merge genuinely separate openings. Dropped ids are listed in `dedupedFrom` on the surviving row.
- **Locations** are parsed into city, region, country and an upper-case `countryCode` from a bundled table. No geocoding, no coordinates, no external service.

## Privacy and contact redaction

- The output has **no contact, recruiter or candidate fields**. Ever.
- `includeDescription` ships on, so a default run carries the ad body. That body is the **employer's own copyrighted advertisement text**; publishing an ad licenses nobody to redistribute it. Set `includeDescription: false` if you would rather not hold ad bodies at all.
- With `redactContacts` on (the default), the body is stripped of email addresses, phone numbers, LinkedIn/Xing/WhatsApp/Calendly/Telegram links and handles, and a person's name where the ad labels it as a contact (`Contact:`, `Hiring manager:`, `Recruiter:`, `Ansprechpartner:`). `descriptionRedacted` records that something was removed.
- **What redaction does not catch, stated plainly — we do not claim "no PII":** a name in running prose with no contact label in front of it survives. Redaction is a *contactability* control, not anonymisation. Anyone named in an ad body gets it removed in 48 hours, unconditionally: [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md) · [PRIVACY.md](https://github.com/moonie0201/ats-jobs/blob/main/PRIVACY.md).
- Nothing is stored outside your own Apify account and nothing is sent to the developer. The Actor opens no outbound connection to us and embeds no credential.

## Pricing

**$0.002 per job row — the only event that costs real money.** Everything else on the Pricing tab is platform floor:

| Event | Price | What it is |
|---|---|---|
| `job` | **$0.002** | One job row delivered to your dataset. This is the price. |
| `apify-actor-start` | $0.00005 | Apify's platform default, charged once per run. |
| `delta-run` | $0.00001 | Instrumentation, recorded once per run when `onlyNewJobs` is on — one cent per thousand monitoring runs. |

Free, always: company summary rows, error rows, jobs removed by your filters, companies never reached because your `maxJobs` cap fired, and companies that failed.

| Run shape | Charged rows | Cost |
|---|---|---|
| One site, 50 jobs (an agent query) | 50 | **$0.10** |
| Ten sites, 1,000 jobs (a backfill) | 1,000 | **$2.00** |
| 500 sites, `onlyNewJobs`, 15 new jobs (daily monitoring) | 15 | **$0.03** |

That is $2 per 1,000 jobs. Set `maxJobs` to bound any run, or `ACTOR_MAX_TOTAL_CHARGE_USD` to bound the spend directly — when that limit is reached the Actor stops pushing, writes `budget_exhausted` summaries and still finishes successfully.

## Monitoring new Lever job postings (delta mode)

Turn on `onlyNewJobs` and give each monitoring task its own `stateKey`. The first run stores the baseline and returns everything; later runs return only ids that were not there before. `stateRetentionDays` (default 90) forgets ids that stopped appearing, so the store cannot grow forever. The state is a key-value store **in your account**, named by you — we never see it.

## Integrations: n8n, Make, Zapier, Clay, Google Sheets, Slack

- **n8n** — the *Apify* node, **Run an Actor**, then *Get dataset items*.
- **Make** — the Apify *Run an Actor* module plus *Watch dataset items*.
- **Zapier** — Apify's *Run Actor* action; map `title`, `company`, `url`, `postedAt` into your CRM.
- **Clay** — call the Actor as an HTTP enrichment step, keyed on the Lever site name.
- **Google Sheets** — export the **Jobs** view as CSV, or push items from n8n/Make.
- **Slack** — filter on `isNew` in delta mode and post new rows to a channel.

## For AI agents and MCP

Agent-friendly by construction: limited permissions, pay-per-event with a single paid event, no Standby mode, typed error rows instead of stack traces, and a `minimal` output profile returning only `title, company, locationRaw, city, countryCode, remote, url, postedAt` to keep token cost down.

```json
{
  "companies": ["palantir"],
  "outputProfile": "minimal",
  "maxJobs": 25,
  "remoteOnly": true
}
```

Set `maxJobs` on every call and `ACTOR_MAX_TOTAL_CHARGE_USD` on the run to cap spend. Errors come back as dataset rows with `recordType: "error"` and a machine-readable `status`, so a tool call never has to parse a traceback.

## Limitations you should know before you buy

- **Lever site names are case-sensitive.** `palantir` works, `Palantir` returns 404. This is the single most common cause of an empty run.
- `api.lever.co` is rate-limited to **1 request per second** on our side, so very large company lists take longer here than on the other ATS listings.
- Structured salary is present only where the employer filled `salaryRange` in; many Lever boards leave it empty and you get `null` rather than a guess.
- An EU-resident board answers on `api.eu.lever.co`. We try the global host first and fall back automatically, so an EU-only board costs one extra request, not an error.
- This listing reads **Lever only**. A Greenhouse board token or an `ashbyhq.com` URL is refused with a free error row naming the right Actor.
- There is no keyword search without a company list.

## Related Actors

`ats-jobs-scraper` reads all six providers (Greenhouse, Lever, Ashby, Recruitee, Rippling, Personio) in one run with the same schema and the same price. Sibling per-ATS listings exist for Greenhouse and Ashby.

## FAQ

**Is this legal?** We call Lever's own public, unauthenticated postings API, we honour documented rate limits, we do not scrape career pages, we use no proxies and we touch no login-walled site. Copyright is a separate question and we will not pretend otherwise: the description body stays the employer's copyrighted text, and any rightholder who wants it out gets it out in 48 hours — [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md).

**Do I need an API key?** No. The endpoint requires no credential and the Actor stores none.

**How fresh is the data?** Fetched live at run time. No index, no cache between you and the board.

**Where do I find a site name?** It is the path segment in the careers URL — `https://jobs.lever.co/palantir` → `palantir`. You can paste the whole URL instead. Copy the casing exactly.

**My board returns 404.** Check the casing first, then confirm the company is really on Lever. EU boards are handled automatically.

**Can I get salaries for every job?** No. `salaryRange` depends entirely on what the employer published.

**How do I schedule it daily?** Apify Schedules, with `onlyNewJobs: true` and a dedicated `stateKey`.

**How do I export to CSV?** Storage → the **Jobs** view → Export → CSV.

## Support and issues

Open an issue on the Actor's Issues tab or in the public GitHub repository at <https://github.com/moonie0201/ats-jobs>. Every issue gets a reply within 14 days, usually within 48 hours. Bug reports that include the run id and the input are fixed fastest.

**Removal, takedown, copyright or privacy requests jump the queue and are answered within 48 hours:** [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md) · [PRIVACY.md](https://github.com/moonie0201/ats-jobs/blob/main/PRIVACY.md).

## Disclaimer

This Actor is unofficial. It is not affiliated with, endorsed by, or sponsored by Lever, Inc. It uses Lever's public postings API to retrieve publicly published job advertisements. All trademarks belong to their respective owners.

Job advertisement text remains the copyright of the employer that wrote it. The structured fields this Actor produces are factual. Removal requests are honoured within 48 hours: [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md).
