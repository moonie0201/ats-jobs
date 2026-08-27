# Ashby Jobs API Scraper

> **Unofficial.** Not affiliated with, endorsed by or sponsored by Ashby, Inc.
> It calls Ashby's own public job-board API. All trademarks belong to their owners.
> **Removal requests:** [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md)
> — honoured in 48 hours. **Privacy:** [PRIVACY.md](https://github.com/moonie0201/ats-jobs/blob/main/PRIVACY.md).

Live job postings straight from the **Ashby public job-board API** — `api.ashbyhq.com/posting-api/job-board/{name}` — normalized into one flat schema with structured compensation, a real remote flag, parsed locations and a stable job id. You paste Ashby job-board names (`openai`) or Ashby careers URLs; you get clean job rows back. Seven filters run **before** anything is billed, company summaries and error rows are free, and `onlyNewJobs` keeps a baseline in your own key-value store so a daily monitoring run returns only what changed. No scraping, no headless browser, no proxies, no API keys.

**We index nothing you buy.** There is no "jobs in our database" number here, because there is no index between you and the board: every row in your run was fetched from that employer's Ashby board seconds earlier, never served from a crawl of unknown age. And because you are billed only for job rows you actually receive, a 500-company daily watch with `onlyNewJobs` costs a couple of cents a run.

**What this is not:** no keyword search without a company list; and this listing reads **Ashby only** — an entry for another ATS comes back as a free error row.

## What this Ashby jobs API does

- **Endpoint** — [`api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true`](https://developers.ashbyhq.com/docs/public-job-posting-api)
- **Job-board name** — the slug in your careers URL: `https://jobs.ashbyhq.com/openai` → `openai`
- **Compensation** — the cleanest structured pay data of any provider in this family: `compensation.compensationTiers` becomes `salaryMin`, `salaryMax`, `salaryCurrency` and `salaryInterval` with `salarySource: "ats"`
- **Remote flag** — Ashby's `isRemote` becomes a real `remote` boolean with `remoteSource: "ats"`
- **Descriptions** — the full ad body, inline in the same response, on by default at no extra cost

That endpoint is **Ashby's own public job-board API**: the same one a company's careers page calls to render itself, documented by Ashby and requiring no credential. That is a different product from career-page scraping — no headless browser, no residential proxy, no bot walls, nothing that breaks when a marketing site is restyled.

Rows you are **not** charged for: company summaries, error rows, jobs removed by your filters, companies never reached because your `maxJobs` cap fired, and companies that failed.

## Supported endpoint and what Ashby actually gives you

| Field | Ashby | Note |
|---|---|---|
| Structured salary | **Yes** — `compensationTiers` | The best of the six providers we support; intervals arrive as `"1 YEAR"` and are normalized to `year` |
| Remote flag | **Yes** — `isRemote` | Stated outright by the board, `remoteSource: "ats"` |
| Description | **Yes**, inline | Both HTML and plain text are available from the same response |
| Employment type | **Yes** — `employmentType` | Normalized to `full_time` / `part_time` / `contract` / `temporary` / `internship` / `other`, `employmentTypeSource: "ats"` |
| Department / team | **Yes** | `department` and `team` both populated where Ashby has them |
| Job URL | Yes | `jobUrl`, plus `applyUrl` where Ashby exposes one |
| Posted date | **Yes** — `publishedAt` | Exposed as `postedAt` with `postedAtSource` |
| Multi-location | **Yes** — `secondaryLocations` | Kept in a sorted `locations[]` array rather than duplicated into extra rows |

## How to use the Ashby job board API

1. **Paste your board names.** One per line: `openai`, or the full URL `https://jobs.ashbyhq.com/openai`.
2. **Set your filters.** Title keywords, excluded titles, location, remote-only, departments, employment types, posted-after. They all run locally on the fetched JSON, before billing.
3. **Run it, or schedule it.** For monitoring, turn on `onlyNewJobs` and give the task its own `stateKey`; the first run stores the baseline and later runs return only what is new.

A minimal run — `["openai"]` with `maxJobs: 50` — finishes in seconds and costs ten cents.

## Coming from fantastic-jobs, jobo, bovi or webdata_labs

Saved input from another Ashby Actor runs here unchanged. These input keys are accepted as aliases for `companies`, first non-empty wins, and an explicit `companies` always wins over an alias:

| Their key | Here |
|---|---|
| `jobBoardNames`, `boardTokens`, `siteNames` | `companies` |
| `queries`, `companyUrls`, `startUrls` | `companies` |
| `subdomains`, `companyIdentifiers` | `companies` |

**What you gain:** live boards instead of an index of unknown age; `postedAfter` and per-company filtering that run *before* billing, so filtered-out jobs are free; structured compensation with currency and interval separated rather than a pay string; no contact, recruiter or candidate fields anywhere in the output; the full ad body on by default with contact redaction on by default; and a `trackedSince` date on every company row once `onlyNewJobs` is on.

**What you lose, stated plainly:** there is no keyword search without a company list. If "find me every Rust job anywhere" is your requirement, an aggregator fits better than this Actor. And this listing is Ashby-only by design — if you need Greenhouse, Lever, Recruitee, Rippling or Personio in the same run, use the multi-ATS Actor `ats-jobs-scraper` instead of running four listings.

## Input

```json
{
  "companies": ["openai", "https://jobs.ashbyhq.com/ramp"],
  "maxJobs": 1000,
  "titleKeywords": ["engineer", "data"],
  "excludeTitleKeywords": ["intern"],
  "locationKeywords": ["New York", "Remote"],
  "remoteOnly": false,
  "employmentTypes": ["full_time"],
  "postedAfter": "7 days",
  "includeDescription": true,
  "descriptionFormat": "text",
  "redactContacts": true,
  "outputProfile": "full",
  "onlyNewJobs": false,
  "stateKey": "ashby-jobs-state-default"
}
```

| Field | Type | Default | Effect on cost |
|---|---|---|---|
| `companies` | array | required | Board names or careers URLs. More companies, more charged rows |
| `maxJobs` | integer | 1000 | **Your main cost control.** Hard stop after this many charged rows; `0` = no limit |
| `maxJobsPerCompany` | integer | 0 | Stops one enterprise board eating the whole budget |
| `titleKeywords` / `excludeTitleKeywords` | array | `[]` | Filtered-out jobs are free |
| `locationKeywords` | array | `[]` | Matches raw location text and parsed city, region, country |
| `remoteOnly` | boolean | false | Keeps only `remote: true` — reliable on Ashby, which states it |
| `departments` | array | `[]` | Substring match on the Ashby department or team |
| `employmentTypes` | array | `[]` | Ashby reports a type on most postings |
| `strictEmploymentType` | boolean | false | Keeps only ATS-confirmed types — usually safe here |
| `postedAfter` | string | null | `2026-08-01` or `7 days`. Jobs with no date are **kept** |
| `includeDescription` | boolean | **true** | Bigger items, same price — the body costs nothing extra |
| `redactContacts` | boolean | true | Strips contact details from description bodies |
| `outputProfile` | string | `full` | `minimal` cuts token cost for AI agents |
| `dedupe` | string | `id` | `content` also merges same title + company + location + requisition id |
| `onlyNewJobs` | boolean | false | Returns only ids not in your state store |
| `stateKey` | string | `ashby-jobs-state-default` | Name of the key-value store holding seen ids |
| `maxConcurrency` | integer | 8 | Speed only; per-host rate is capped separately at 2 rps |

## Output

One `job` row per posting (charged), plus free `company_summary` and `error` rows. The dataset schema is identical to the multi-ATS Actor's, so a pipeline can read both without a branch.

```json
{
  "recordType": "job",
  "id": "ashby:openai:0f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f",
  "provider": "ashby",
  "companySlug": "openai",
  "company": "OpenAI",
  "title": "Research Engineer, Alignment",
  "department": "Research",
  "team": "Alignment",
  "locationRaw": "San Francisco, CA",
  "city": "San Francisco",
  "region": "CA",
  "country": "United States",
  "countryCode": "US",
  "remote": false,
  "remoteSource": "ats",
  "employmentType": "full_time",
  "employmentTypeSource": "ats",
  "salaryMin": 245000,
  "salaryMax": 385000,
  "salaryCurrency": "USD",
  "salaryInterval": "year",
  "salarySource": "ats",
  "url": "https://jobs.ashbyhq.com/openai/0f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f",
  "postedAt": "2026-08-19T00:00:00Z",
  "postedAtSource": "published_at",
  "descriptionRedacted": false,
  "isNew": true,
  "scrapedAt": "2026-08-26T03:00:12Z"
}
```

Two dataset views are provided: **Jobs** (spreadsheet-ready postings) and **Company summaries**. Export either as JSON, CSV, Excel or XML from the Storage tab.

**What `null` means: we could not determine it.** We never guess remote status, employment type, seniority or salary. A null field is the honest answer, and far more useful than an invented one when you are filtering thousands of rows.

## Filters, deduplication and normalization

- **`remote`** comes from Ashby's `isRemote` where present (`remoteSource: "ats"`); otherwise from the location text or the title, and `null` when nothing said. `remoteOnly` keeps only positively-confirmed remote jobs.
- **Salary** comes from `compensationTiers` first (`salarySource: "ats"`). Ashby's `"1 YEAR"` / `"1 MONTH"` interval strings are normalized to `year` / `month`. Only when there is no structured tier do we run a conservative regex over the salary text (`salarySource: "parsed"`), with rejection gates for equity, bonuses, funding rounds, `401(k)`, date ranges and phone numbers. **Never an LLM.**
- **Employment type** is normalized from Ashby's `employmentType` to `full_time`, `part_time`, `contract`, `temporary`, `internship` or `other`. `strictEmploymentType` keeps only ATS-confirmed ones.
- **Dedupe by id** is always on. **By content** additionally merges rows with the same title, company, raw location and requisition id — useful for boards that list a role once per office, but it can merge genuinely separate openings. Dropped ids are listed in `dedupedFrom` on the surviving row.
- **Locations** are parsed into city, region, country and an upper-case `countryCode` from a bundled table. Ashby's `secondaryLocations` are kept in a sorted `locations[]` array. No geocoding, no coordinates, no external service.

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
| One board, 50 jobs (an agent query) | 50 | **$0.10** |
| Ten boards, 1,000 jobs (a backfill) | 1,000 | **$2.00** |
| 500 boards, `onlyNewJobs`, 15 new jobs (daily monitoring) | 15 | **$0.03** |

That is $2 per 1,000 jobs. Set `maxJobs` to bound any run, or `ACTOR_MAX_TOTAL_CHARGE_USD` to bound the spend directly — when that limit is reached the Actor stops pushing, writes `budget_exhausted` summaries and still finishes successfully.

## Monitoring new Ashby job postings (delta mode)

Turn on `onlyNewJobs` and give each monitoring task its own `stateKey`. The first run stores the baseline and returns everything; later runs return only ids that were not there before. `stateRetentionDays` (default 90) forgets ids that stopped appearing, so the store cannot grow forever. The state is a key-value store **in your account**, named by you — we never see it.

## Integrations: n8n, Make, Zapier, Clay, Google Sheets, Slack

- **n8n** — the *Apify* node, **Run an Actor**, then *Get dataset items*.
- **Make** — the Apify *Run an Actor* module plus *Watch dataset items*.
- **Zapier** — Apify's *Run Actor* action; map `title`, `company`, `url`, `postedAt` into your CRM.
- **Clay** — call the Actor as an HTTP enrichment step, keyed on the Ashby board name.
- **Google Sheets** — export the **Jobs** view as CSV, or push items from n8n/Make.
- **Slack** — filter on `isNew` in delta mode and post new rows to a channel.

## For AI agents and MCP

Agent-friendly by construction: limited permissions, pay-per-event with a single paid event, no Standby mode, typed error rows instead of stack traces, and a `minimal` output profile returning only `title, company, locationRaw, city, countryCode, remote, url, postedAt` to keep token cost down.

```json
{
  "companies": ["openai"],
  "outputProfile": "minimal",
  "maxJobs": 25,
  "titleKeywords": ["research engineer"]
}
```

Set `maxJobs` on every call and `ACTOR_MAX_TOTAL_CHARGE_USD` on the run to cap spend. Errors come back as dataset rows with `recordType: "error"` and a machine-readable `status`, so a tool call never has to parse a traceback.

## Limitations you should know before you buy

- Structured compensation is present only where the employer published it. Ashby carries it more often than any other provider we support, but "more often" is not "always" and we return `null` rather than a guess.
- Ashby board names are the slug in the `jobs.ashbyhq.com` URL. A wrong name returns a free `not_found` error row, not a charge.
- Some Ashby boards are configured private or embedded behind the employer's own domain; the public posting API answers only for boards the employer published.
- Seniority is not reported by Ashby, so `seniority` is `null` on every row. We never infer it.
- This listing reads **Ashby only**. A Greenhouse board token or a `jobs.lever.co` URL is refused with a free error row naming the right Actor.
- There is no keyword search without a company list.

## Related Actors

`ats-jobs-scraper` reads all six providers (Greenhouse, Lever, Ashby, Recruitee, Rippling, Personio) in one run with the same schema and the same price. Sibling per-ATS listings exist for Greenhouse and Lever.

## FAQ

**Is this legal?** We call Ashby's own public, unauthenticated job-board API, we honour documented rate limits, we do not scrape career pages, we use no proxies and we touch no login-walled site. Copyright is a separate question and we will not pretend otherwise: the description body stays the employer's copyrighted text, and any rightholder who wants it out gets it out in 48 hours — [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md).

**Do I need an API key?** No. The endpoint requires no credential and the Actor stores none.

**How fresh is the data?** Fetched live at run time. No index, no cache between you and the board.

**Where do I find a board name?** It is the path segment in the careers URL — `https://jobs.ashbyhq.com/openai` → `openai`. You can paste the whole URL instead.

**Why is the salary interval `year` when Ashby said `1 YEAR`?** We normalize it, so a filter or a spreadsheet does not have to parse two formats.

**Can I get salaries for every job?** No. Compensation depends entirely on what the employer published.

**How do I schedule it daily?** Apify Schedules, with `onlyNewJobs: true` and a dedicated `stateKey`.

**How do I export to CSV?** Storage → the **Jobs** view → Export → CSV.

## Support and issues

Open an issue on the Actor's Issues tab or in the public GitHub repository at <https://github.com/moonie0201/ats-jobs>. Every issue gets a reply within 14 days, usually within 48 hours. Bug reports that include the run id and the input are fixed fastest.

**Removal, takedown, copyright or privacy requests jump the queue and are answered within 48 hours:** [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md) · [PRIVACY.md](https://github.com/moonie0201/ats-jobs/blob/main/PRIVACY.md).

## Disclaimer

This Actor is unofficial. It is not affiliated with, endorsed by, or sponsored by Ashby, Inc. It uses Ashby's public job-board API to retrieve publicly published job advertisements. All trademarks belong to their respective owners.

Job advertisement text remains the copyright of the employer that wrote it. The structured fields this Actor produces are factual. Removal requests are honoured within 48 hours: [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md).
