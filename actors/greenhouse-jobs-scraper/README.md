# Greenhouse Jobs API Scraper

> **Unofficial.** Not affiliated with, endorsed by or sponsored by Greenhouse Software, Inc.
> It calls Greenhouse's own public job-board API. All trademarks belong to their owners.
> **Removal requests:** [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md)
> — honoured in 48 hours. **Privacy:** [PRIVACY.md](https://github.com/moonie0201/ats-jobs/blob/main/PRIVACY.md).

Live job postings straight from the **Greenhouse public job board API** — `boards-api.greenhouse.io` — normalized into one flat schema with structured pay-transparency ranges, parsed locations and a stable job id. You paste board tokens (`anthropic`) or Greenhouse careers URLs; you get clean job rows back. Seven filters run **before** anything is billed, company summaries and error rows are free, and `onlyNewJobs` keeps a baseline in your own key-value store so a daily monitoring run returns only what changed. No scraping, no headless browser, no proxies, no API keys.

**We index nothing you buy.** There is no "jobs in our database" number here, because there is no index between you and the board: every row in your run was fetched from that employer's Greenhouse board seconds earlier, never served from a crawl of unknown age. And because you are billed only for job rows you actually receive, a 500-company daily watch with `onlyNewJobs` costs a couple of cents a run.

**What this is not:** no keyword search without a company list; `remote` is usually `null` on Greenhouse because the board never says; and this listing reads **Greenhouse only** — an entry for another ATS comes back as a free error row.

## What this Greenhouse jobs API does

- **Endpoint** — [`boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true&pay_transparency=true`](https://developers.greenhouse.io/job-board.html)
- **Board token** — the slug in your board URL: `https://job-boards.greenhouse.io/anthropic` → `anthropic`
- **Pay transparency** — Greenhouse's `pay_input_ranges` become `salaryMin`, `salaryMax`, `salaryCurrency` and `salaryInterval`, with `salarySource: "ats"`
- **Descriptions** — the full ad body, inline in the same response, on by default at no extra cost

That endpoint is **Greenhouse's own public job-board API**: the same one a company's careers page calls to render itself, documented by Greenhouse and requiring no credential. That is a different product from career-page scraping — no headless browser, no residential proxy, no bot walls, nothing that breaks when a marketing site is restyled.

Rows you are **not** charged for: company summaries, error rows, jobs removed by your filters, companies never reached because your `maxJobs` cap fired, and companies that failed.

## Supported endpoint and what Greenhouse actually gives you

| Field | Greenhouse | Note |
|---|---|---|
| Structured salary | **Yes** — `pay_input_ranges` | Boards publishing several locale ranges: we pick the one matching the job's own country or currency |
| Remote flag | **No** | `remote` is `null` unless the location text or the title says so; we never guess |
| Description | **Yes**, inline | The employer's own copyrighted ad body; contacts redacted by default |
| Employment type | **No** | Where you see one it was inferred from the title, and `employmentTypeSource` says `title` |
| Department | **Yes** | `team` is always `null` — Greenhouse does not report it |
| Job URL | Yes | Often the company's own careers domain, not a `greenhouse.io` link — that is the employer's configuration |
| Posted date | **Yes** | `first_published`, exposed as `postedAt` with `postedAtSource` |

## How to use the Greenhouse job board API

1. **Paste your board tokens.** One per line: `anthropic`, or the full URL `https://job-boards.greenhouse.io/anthropic`. The old `boards.greenhouse.io` host and the `embed/job_board?for=` form both resolve too.
2. **Set your filters.** Title keywords, excluded titles, location, remote-only, departments, employment types, posted-after. They all run locally on the fetched JSON, before billing.
3. **Run it, or schedule it.** For monitoring, turn on `onlyNewJobs` and give the task its own `stateKey`; the first run stores the baseline and later runs return only what is new.

A minimal run — `["anthropic"]` with `maxJobs: 50` — finishes in seconds and costs ten cents.

## Coming from fantastic-jobs, jobo, bovi or webdata_labs

Saved input from another Greenhouse Actor runs here unchanged. These input keys are accepted as aliases for `companies`, first non-empty wins, and an explicit `companies` always wins over an alias:

| Their key | Here |
|---|---|
| `boardTokens`, `siteNames`, `jobBoardNames` | `companies` |
| `queries`, `companyUrls`, `startUrls` | `companies` |
| `subdomains`, `companyIdentifiers` | `companies` |

**What you gain:** live boards instead of an index of unknown age; `postedAfter` and per-company filtering that run *before* billing, so filtered-out jobs are free; structured salary with currency and interval separated rather than a pay string; no contact, recruiter or candidate fields anywhere in the output; the full ad body on by default with contact redaction on by default; and a `trackedSince` date on every company row once `onlyNewJobs` is on.

**What you lose, stated plainly:** there is no keyword search without a company list. If "find me every Rust job anywhere" is your requirement, an aggregator fits better than this Actor. And this listing is Greenhouse-only by design — if you need Lever, Ashby, Recruitee, Rippling or Personio in the same run, use the multi-ATS Actor `ats-jobs-scraper` instead of running four listings.

## Input

```json
{
  "companies": ["anthropic", "https://job-boards.greenhouse.io/stripe"],
  "maxJobs": 1000,
  "titleKeywords": ["engineer", "data"],
  "excludeTitleKeywords": ["intern"],
  "locationKeywords": ["Berlin", "Germany", "Remote"],
  "postedAfter": "7 days",
  "includeDescription": true,
  "descriptionFormat": "text",
  "redactContacts": true,
  "outputProfile": "full",
  "onlyNewJobs": false,
  "stateKey": "greenhouse-jobs-state-default"
}
```

| Field | Type | Default | Effect on cost |
|---|---|---|---|
| `companies` | array | required | Board tokens or careers URLs. More companies, more charged rows |
| `maxJobs` | integer | 1000 | **Your main cost control.** Hard stop after this many charged rows; `0` = no limit |
| `maxJobsPerCompany` | integer | 0 | Stops one enterprise board eating the whole budget |
| `titleKeywords` / `excludeTitleKeywords` | array | `[]` | Filtered-out jobs are free |
| `locationKeywords` | array | `[]` | Matches raw location text and parsed city, region, country |
| `remoteOnly` | boolean | false | Keeps only `remote: true` — rare on Greenhouse, see Limitations |
| `departments` | array | `[]` | Substring match on the Greenhouse department name |
| `employmentTypes` | array | `[]` | Greenhouse reports none, so these are title-inferred |
| `postedAfter` | string | null | `2026-08-01` or `7 days`. Jobs with no date are **kept** |
| `includeDescription` | boolean | **true** | Bigger items, same price — the body costs nothing extra |
| `redactContacts` | boolean | true | Strips contact details from description bodies |
| `outputProfile` | string | `full` | `minimal` cuts token cost for AI agents |
| `dedupe` | string | `id` | `content` also merges same title + company + location + requisition id |
| `onlyNewJobs` | boolean | false | Returns only ids not in your state store |
| `stateKey` | string | `greenhouse-jobs-state-default` | Name of the key-value store holding seen ids |
| `maxConcurrency` | integer | 8 | Speed only; per-host rate is capped separately at 2 rps |

## Output

One `job` row per posting (charged), plus free `company_summary` and `error` rows. The dataset schema is identical to the multi-ATS Actor's, so a pipeline can read both without a branch.

```json
{
  "recordType": "job",
  "id": "greenhouse:anthropic:4019283",
  "provider": "greenhouse",
  "companySlug": "anthropic",
  "company": "Anthropic",
  "title": "Senior Backend Engineer, Inference",
  "department": "Engineering",
  "team": null,
  "locationRaw": "San Francisco, CA",
  "city": "San Francisco",
  "region": "CA",
  "country": "United States",
  "countryCode": "US",
  "remote": null,
  "employmentType": null,
  "salaryMin": 300000,
  "salaryMax": 405000,
  "salaryCurrency": "USD",
  "salaryInterval": "year",
  "salarySource": "ats",
  "url": "https://job-boards.greenhouse.io/anthropic/jobs/4019283",
  "postedAt": "2026-08-19T00:00:00Z",
  "postedAtSource": "first_published",
  "descriptionRedacted": true,
  "isNew": true,
  "scrapedAt": "2026-08-26T03:00:12Z"
}
```

Two dataset views are provided: **Jobs** (spreadsheet-ready postings) and **Company summaries**. Export either as JSON, CSV, Excel or XML from the Storage tab.

**What `null` means: we could not determine it.** We never guess remote status, employment type, seniority or salary. A null field is the honest answer, and far more useful than an invented one when you are filtering thousands of rows.

## Filters, deduplication and normalization

- **`remote: null`** means neither the board, the location text nor the title said anything. Greenhouse has no remote flag at all, so nulls are the norm here. `remoteSource` tells you which rule fired when it is not null.
- **Salary** comes from `pay_input_ranges` first (`salarySource: "ats"`). Only when there is none do we run a conservative regex over the salary text (`salarySource: "parsed"`), with rejection gates for equity, bonuses, funding rounds, `401(k)`, date ranges and phone numbers. **Never an LLM.**
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
| One board, 50 jobs (an agent query) | 50 | **$0.10** |
| Ten boards, 1,000 jobs (a backfill) | 1,000 | **$2.00** |
| 500 boards, `onlyNewJobs`, 15 new jobs (daily monitoring) | 15 | **$0.03** |

That is $2 per 1,000 jobs. Set `maxJobs` to bound any run, or `ACTOR_MAX_TOTAL_CHARGE_USD` to bound the spend directly — when that limit is reached the Actor stops pushing, writes `budget_exhausted` summaries and still finishes successfully.

## Monitoring new Greenhouse job postings (delta mode)

Turn on `onlyNewJobs` and give each monitoring task its own `stateKey`. The first run stores the baseline and returns everything; later runs return only ids that were not there before. `stateRetentionDays` (default 90) forgets ids that stopped appearing, so the store cannot grow forever. The state is a key-value store **in your account**, named by you — we never see it.

## Integrations: n8n, Make, Zapier, Clay, Google Sheets, Slack

- **n8n** — the *Apify* node, **Run an Actor**, then *Get dataset items*.
- **Make** — the Apify *Run an Actor* module plus *Watch dataset items*.
- **Zapier** — Apify's *Run Actor* action; map `title`, `company`, `url`, `postedAt` into your CRM.
- **Clay** — call the Actor as an HTTP enrichment step, keyed on the Greenhouse board token.
- **Google Sheets** — export the **Jobs** view as CSV, or push items from n8n/Make.
- **Slack** — filter on `isNew` in delta mode and post new rows to a channel.

## For AI agents and MCP

Agent-friendly by construction: limited permissions, pay-per-event with a single paid event, no Standby mode, typed error rows instead of stack traces, and a `minimal` output profile returning only `title, company, locationRaw, city, countryCode, remote, url, postedAt` to keep token cost down.

```json
{
  "companies": ["anthropic"],
  "outputProfile": "minimal",
  "maxJobs": 25,
  "titleKeywords": ["research engineer"]
}
```

Set `maxJobs` on every call and `ACTOR_MAX_TOTAL_CHARGE_USD` on the run to cap spend. Errors come back as dataset rows with `recordType: "error"` and a machine-readable `status`, so a tool call never has to parse a traceback.

## Limitations you should know before you buy

- **Greenhouse has no remote flag**, so `remote` is usually `null`. We do not guess.
- **Greenhouse reports no employment type and never reports `team`.** Where `employmentType` is set, `employmentTypeSource` will say `title`.
- The job `url` is frequently the company's own careers domain rather than a `greenhouse.io` link — that is the employer's configuration, and `applyUrl` duplicates it because Greenhouse exposes no separate apply link.
- Board tokens must be exact. A wrong token returns a free `not_found` error row, not a charge.
- This listing reads **Greenhouse only**. `lever:palantir` or an `ashbyhq.com` URL is refused with a free error row naming the right Actor.
- There is no keyword search without a company list.

## Related Actors

`ats-jobs-scraper` reads all six providers (Greenhouse, Lever, Ashby, Recruitee, Rippling, Personio) in one run with the same schema and the same price. Sibling per-ATS listings exist for Lever and Ashby.

## FAQ

**Is this legal?** We call Greenhouse's own public, unauthenticated job-board API, we honour documented rate limits, we do not scrape career pages, we use no proxies and we touch no login-walled site. Copyright is a separate question and we will not pretend otherwise: the description body stays the employer's copyrighted text, and any rightholder who wants it out gets it out in 48 hours — [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md).

**Do I need an API key?** No. The endpoint requires no credential and the Actor stores none.

**How fresh is the data?** Fetched live at run time. No index, no cache between you and the board.

**Where do I find a board token?** It is the path segment in the board URL — `https://job-boards.greenhouse.io/anthropic` → `anthropic`. You can paste the whole URL instead.

**Why is `remote` null?** Because Greenhouse never said. See Limitations.

**Can I get salaries for every job?** No. Pay-transparency ranges depend entirely on what the employer published.

**How do I schedule it daily?** Apify Schedules, with `onlyNewJobs: true` and a dedicated `stateKey`.

**How do I export to CSV?** Storage → the **Jobs** view → Export → CSV.

## Support and issues

Open an issue on the Actor's Issues tab or in the public GitHub repository at <https://github.com/moonie0201/ats-jobs>. Every issue gets a reply within 14 days, usually within 48 hours. Bug reports that include the run id and the input are fixed fastest.

**Removal, takedown, copyright or privacy requests jump the queue and are answered within 48 hours:** [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md) · [PRIVACY.md](https://github.com/moonie0201/ats-jobs/blob/main/PRIVACY.md).

## Disclaimer

This Actor is unofficial. It is not affiliated with, endorsed by, or sponsored by Greenhouse Software, Inc. It uses Greenhouse's public job-board API to retrieve publicly published job advertisements. All trademarks belong to their respective owners.

Job advertisement text remains the copyright of the employer that wrote it. The structured fields this Actor produces are factual. Removal requests are honoured within 48 hours: [TAKEDOWN.md](https://github.com/moonie0201/ats-jobs/blob/main/TAKEDOWN.md).
